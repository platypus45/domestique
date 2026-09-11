"""v3.11.5 — missed sessions are registered across plan-week boundaries and
their load is recycled only where the evidence allows.

Owner's plan, 2026-09-11: weeks run Fri→Thu; Thursday's threshold was missed;
on Friday the reconcile / refit / reschedule tiers only looked at the plan-week
containing today, so the miss was never registered, never re-owed, never
explained. See plans/IP_missed_load_recycle.md for the literature.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

import training_planner as tp


@pytest.fixture(scope="module")
def library():
    lib = tp.load_workout_library()
    assert len(lib) >= 4000
    return lib


def _s(day, stype, dur, tss, status="pending", **kw):
    return tp.PlannedSession(day=day, day_name=day.strftime("%A"), session_type=stype,
                             duration_min=dur, tss_estimate=tss, description="", status=status, **kw)


def _w(start, sessions, week_num, tss_target=300.0, is_stepback=False, phase="build1"):
    return tp.PlannedWeek(week_num=week_num, start=start, end=start + timedelta(days=6), phase=phase,
                          tss_target=tss_target, is_stepback=is_stepback, sessions=sessions,
                          hit_per_week=tp.get_budget_for_phase(phase).hit_count_max)


def _goal(cap_h: float):
    """Every day available with the same cap (the owner: 1.0 h)."""
    return tp.Goal(goal_type="general", hours_per_week=cap_h * 7,
                   max_weekday_hours=cap_h, max_weekend_hours=cap_h,
                   rest_days=[], available_days=[0, 1, 2, 3, 4, 5, 6],
                   daily_max_hours={d: cap_h for d in range(7)})


def _friday_anchored(today):
    """Plan weeks that run Fri→Thu with ``today`` the first day of the current week."""
    assert today.weekday() == 4
    prev_start, cur_start = today - timedelta(days=7), today
    return prev_start, cur_start


TODAY = date(2026, 9, 11)   # a Friday, the owner's case


# ── the refit sees a miss from the previous plan-week ────────────────────────

def test_refit_owes_a_previous_week_miss_into_a_safe_day(library):
    prev_start, cur_start = _friday_anchored(TODAY)
    thu = prev_start + timedelta(days=6)
    prev = _w(prev_start, [_s(prev_start + timedelta(days=i), "z2", 60, 45, status="missed") for i in range(6)]
              + [_s(thu, "threshold", 64, 80, status="missed", zwo_file="threshold_3x10min_95pct_64min.zwo")], 4)
    cur = _w(cur_start, [_s(cur_start + timedelta(days=i), "z2", 90, 68) for i in range(7)], 5)
    weeks = [prev, cur]
    _, info = tp.refit_remaining_week(_goal(1.5), weeks, TODAY, owed_missed=[prev.sessions[6]],
                                      prev_done_hard_days=[])
    assert info["missed_dates"] == [thu.isoformat()]          # the owed miss is on the latch key
    assert info["missed_dose"] == 80
    assert any(tp._session_is_hit(s) for s in cur.sessions)   # the stimulus was re-owed this week


def test_refit_keeps_48h_from_a_hard_session_done_last_week(library):
    prev_start, cur_start = _friday_anchored(TODAY)
    thu = prev_start + timedelta(days=6)
    wed = prev_start + timedelta(days=5)
    prev = _w(prev_start, [_s(prev_start + timedelta(days=i), "z2", 60, 45, status="done") for i in range(5)]
              + [_s(wed, "vo2max", 60, 75, status="done"), _s(thu, "threshold", 64, 80, status="missed")], 4)
    cur = _w(cur_start, [_s(cur_start + timedelta(days=i), "z2", 90, 68) for i in range(7)], 5)
    _, info = tp.refit_remaining_week(_goal(1.5), [prev, cur], TODAY, owed_missed=[prev.sessions[6]],
                                      prev_done_hard_days=[wed])
    for d in info.get("promoted_days") or []:
        assert (date.fromisoformat(d) - wed).days >= 2, "re-owed hard landed within 48 h of a hard done last week"


# ── recycling the load, bounded ──────────────────────────────────────────────

def _refit_info(missed_dates, dose, promoted=0.0, blocked=""):
    return {"action": "no_change", "missed_dates": missed_dates, "missed_dose": dose,
            "promoted_tss": promoted, "promoted_days": [], "promote_blocked": blocked, "refit_days": []}


def test_volume_spreads_over_easy_days_within_caps_and_growth(library):
    _, cur_start = _friday_anchored(TODAY)
    thu = cur_start - timedelta(days=1)
    cur = _w(cur_start, [_s(cur_start + timedelta(days=i), "z2", 60, 45) for i in range(7)], 5)
    prev = _w(cur_start - timedelta(days=7), [_s(thu, "threshold", 64, 80, status="missed")], 4)
    ledger = tp.recycle_missed_load([prev, cur], 1, TODAY, _goal(1.5), _refit_info([thu.isoformat()], 80),
                                    library=library, availability=None)
    rows = ledger["placed_this_week"]
    assert len(rows) >= 2, "volume must spread over more than one day (Foster)"
    assert all(r["min"] <= 30 for r in rows)                   # ≤ +50 % of a 60-min day
    assert all(s.duration_min <= 90 for s in cur.sessions)     # never past the 1.5 h cap
    placed = sum(r["tss"] for r in rows)
    assert 0 < placed <= 80 and ledger["dropped_tss"] + placed + ledger["promoted_tss"] >= 80 - 3
    assert prev.sessions[0].refit_note.startswith("missed:")
    assert any("recycled from Thu 10 Sep threshold" in s.refit_note for s in cur.sessions if s.refit_note)


def test_owner_case_no_room_anywhere_is_dropped_with_the_reason(library):
    """1-hour caps every day, Sat VO2 pinned, Tue/Thu hard, no next week: the
    threshold cannot go anywhere — and the card says exactly why."""
    prev_start, cur_start = _friday_anchored(TODAY)
    thu = prev_start + timedelta(days=6)
    prev = _w(prev_start, [_s(thu, "threshold", 64, 80, status="missed")], 4)
    d = lambda i: cur_start + timedelta(days=i)
    cur = _w(cur_start, [
        _s(d(0), "z2", 60, 45), _s(d(1), "vo2max", 60, 75, user_swapped=True), _s(d(2), "z2", 60, 45),
        _s(d(3), "rest", 0, 0), _s(d(4), "sweetspot", 60, 80, user_swapped=True), _s(d(5), "rest", 0, 0),
        _s(d(6), "vo2max", 60, 75, user_swapped=True)], 5)
    weeks = [prev, cur]
    availability = {d(i).isoformat(): {"hours": 1, "type": "available"} for i in range(7)}
    _, info = tp.refit_remaining_week(_goal(1.0), weeks, TODAY, owed_missed=[prev.sessions[0]])
    ledger = tp.recycle_missed_load(weeks, 1, TODAY, _goal(1.0), info, library=library, availability=availability)
    assert [s.session_type for s in cur.sessions] == ["z2", "vo2max", "z2", "rest", "sweetspot", "rest", "vo2max"]
    assert cur.sessions[1].user_swapped and cur.sessions[1].session_type == "vo2max"   # pinned, untouched
    assert cur.sessions[0].session_type == "z2" and cur.sessions[0].duration_min <= 60     # Friday stays easy, within the cap
    # No day may grow past the rider's 1-hour cap; nothing goes to a week that does not exist.
    assert all(s.duration_min <= 60 for s in cur.sessions)
    placed = sum(r["tss"] for r in ledger["placed_this_week"])
    assert ledger["carried_next_week"] is None
    assert ledger["dropped_tss"] == 80 - placed and ledger["dropped_tss"] >= 60
    note = prev.sessions[0].refit_note
    assert note.startswith("missed:") and "TSS dropped" in note
    assert "availability" in note or "hard session" in note or "next week" in note


def test_carry_to_next_week_within_acwr_ramp_and_caps(library):
    _, cur_start = _friday_anchored(TODAY)
    thu = cur_start - timedelta(days=1)
    prev = _w(cur_start - timedelta(days=7), [_s(thu, "threshold", 64, 80, status="missed")], 4)
    cur = _w(cur_start, [_s(cur_start + timedelta(days=i), "z2", 60, 45) for i in range(7)], 5)
    nxt_start = cur_start + timedelta(days=7)
    nxt = _w(nxt_start, [_s(nxt_start + timedelta(days=i), "z2", 60, 45) for i in range(7)], 6, tss_target=315.0)
    goal = _goal(1.0)
    avail_cur = {(cur_start + timedelta(days=i)).isoformat(): {"hours": 1} for i in range(7)}
    avail_nxt = {(nxt_start + timedelta(days=i)).isoformat(): {"hours": 1.5} for i in range(7)}
    # CTL 40 → chronic 280, acute allowed 364, ramp allowed (40+3)×7 = 301; next week planned 315 → headroom 0
    ledger = tp.recycle_missed_load([prev, cur, nxt], 1, TODAY, goal, _refit_info([thu.isoformat()], 80),
                                    library=library, availability={**avail_cur, **avail_nxt}, current_ctl=40.0)
    assert ledger["carried_next_week"] is None and "fitness allows" in " ".join(ledger["reasons"])
    # CTL 60 → ramp allowed (60+4.5)×7 = 451, acute 546; headroom 136 → carry the full 80
    cur2 = _w(cur_start, [_s(cur_start + timedelta(days=i), "z2", 60, 45) for i in range(7)], 5)
    nxt2 = _w(nxt_start, [_s(nxt_start + timedelta(days=i), "z2", 60, 45) for i in range(7)], 6, tss_target=315.0)
    ledger2 = tp.recycle_missed_load([prev, cur2, nxt2], 1, TODAY, goal, _refit_info([thu.isoformat()], 80),
                                     library=library, availability={**avail_cur, **avail_nxt}, current_ctl=60.0)
    carried = ledger2["carried_next_week"]
    assert carried and 60 <= carried["tss"] <= 80 and len(carried["days"]) >= 2
    assert nxt2.tss_target == 315 + carried["tss"]
    assert all(s.duration_min <= 90 for s in nxt2.sessions)
    # a reduced-load next week or a taper window or low form: nothing carried
    for kw in ({"taper_blocked": True}, {"tsb": -30.0}):
        n3 = _w(nxt_start, [_s(nxt_start + timedelta(days=i), "z2", 60, 45) for i in range(7)], 6)
        l3 = tp.recycle_missed_load([prev, _w(cur_start, [_s(cur_start, "z2", 60, 45)], 5), n3], 1, TODAY, goal,
                                    _refit_info([thu.isoformat()], 80), library=library, availability={**avail_cur, **avail_nxt},
                                    current_ctl=60.0, **kw)
        assert l3["carried_next_week"] is None and l3["dropped_tss"] == 80
    n4 = _w(nxt_start, [_s(nxt_start + timedelta(days=i), "z2", 60, 45) for i in range(7)], 6, is_stepback=True)
    l4 = tp.recycle_missed_load([prev, _w(cur_start, [_s(cur_start, "z2", 60, 45)], 5), n4], 1, TODAY, goal,
                                _refit_info([thu.isoformat()], 80), library=library, availability={**avail_cur, **avail_nxt},
                                current_ctl=60.0)
    assert l4["carried_next_week"] is None and "reduced-load" in " ".join(l4["reasons"])


# ── app plumbing ─────────────────────────────────────────────────────────────

def _plan_dict(prev_start, cur_start):
    def sj(day, st, dur, tss, status="pending", **kw):
        return {"day": day.isoformat(), "day_name": day.strftime("%A"), "session_type": st, "duration_min": dur,
                "tss_estimate": tss, "description": "", "zwo_file": "x.zwo" if st != "rest" else "", "status": status, **kw}
    d = lambda base, i: base + timedelta(days=i)
    prev = {"week_num": 4, "start": prev_start.isoformat(), "end": d(prev_start, 6).isoformat(), "phase": "build1", "tss_target": 187,
            "sessions": [sj(d(prev_start, i), "z2", 60, 45, status="missed") for i in range(5)]
            + [sj(d(prev_start, 5), "z2", 60, 45), sj(d(prev_start, 6), "threshold", 64, 80, user_swapped=True)]}
    cur = {"week_num": 5, "start": cur_start.isoformat(), "end": d(cur_start, 6).isoformat(), "phase": "build1", "tss_target": 266,
           "sessions": [sj(d(cur_start, 0), "z2", 60, 31), sj(d(cur_start, 1), "vo2max", 60, 75, user_swapped=True), sj(d(cur_start, 2), "z2", 60, 45),
                        sj(d(cur_start, 3), "rest", 0, 0), sj(d(cur_start, 4), "sweetspot", 60, 80), sj(d(cur_start, 5), "rest", 0, 0),
                        sj(d(cur_start, 6), "vo2max", 60, 75)]}
    return {"goal": {"type": "continuous", "hours_per_week": 5, "available_days": [1, 3, 4, 5, 6], "rest_days": [0, 2],
                     "daily_max_hours": {str(i): (0.0 if i in (0, 2) else 1.0) for i in range(7)}, "max_weekday_hours": 1, "max_weekend_hours": 1},
            "phases": [], "weeks": [prev, cur], "generated": "2026-08-14T09:09:37",
            "availability": {d(cur_start, i).isoformat(): {"hours": 1, "type": "available"} for i in range(7)}}


def test_reconcile_recent_weeks_marks_last_week_misses(monkeypatch):
    import app as app_module
    prev_start, cur_start = _friday_anchored(TODAY)
    plan = _plan_dict(prev_start, cur_start)
    monkeypatch.setattr(app_module, "_collect_week_activities", lambda *a, **k: [])   # no rides in the sandbox
    changed, _ = app_module._reconcile_recent_weeks(plan, TODAY)
    prev = plan["weeks"][0]["sessions"]
    assert prev[5]["status"] == "missed" and prev[6]["status"] == "missed"            # Wed and Thu registered
    assert changed >= 2
    cur = plan["weeks"][1]["sessions"]
    assert all((s.get("status") or "pending") == "pending" for s in cur)             # today and later untouched


def test_recent_missed_and_done_hards_looks_outside_the_current_week():
    import app as app_module
    prev_start, cur_start = _friday_anchored(TODAY)
    plan = _plan_dict(prev_start, cur_start)
    plan["weeks"][0]["sessions"][6]["status"] = "missed"
    plan["weeks"][0]["sessions"][4]["session_type"] = "vo2max"; plan["weeks"][0]["sessions"][4]["status"] = "done"
    cur_dto, _ = app_module._load_current_week_dto(plan, TODAY)
    owed, done = app_module._recent_missed_and_done_hards(plan, TODAY, cur_dto)
    assert owed == [(prev_start + timedelta(days=6)).isoformat()]
    assert done == [prev_start + timedelta(days=4)]


def test_apply_refit_to_plan_owner_case_writes_note_and_ledger(monkeypatch, library):
    import app as app_module
    prev_start, cur_start = _friday_anchored(TODAY)
    plan = _plan_dict(prev_start, cur_start)
    thu = (prev_start + timedelta(days=6)).isoformat()
    plan["weeks"][0]["sessions"][6]["status"] = "missed"
    monkeypatch.setattr(app_module, "_hr_bias", lambda: False)
    refit_info, ledger = app_module._apply_refit_to_plan(
        plan, TODAY, owed_days=[thu], prev_done_hard_days=[],
        ctx={"current_ctl": 34.2, "recent_weekly_tss": 81.0, "tsb": -3.0, "taper_blocked": False,
             "availability": plan["availability"]})
    assert ledger is not None and ledger["missed_dates"] == [thu] and ledger["missed_dose"] == 80
    placed = sum(r["tss"] for r in ledger["placed_this_week"])
    assert ledger["carried_next_week"] is None                                        # no next week in this plan
    assert ledger["dropped_tss"] == 80 - placed and plan["missed_recycle"]["dropped_tss"] == ledger["dropped_tss"]
    thu_sj = plan["weeks"][0]["sessions"][6]
    assert thu_sj["refit_note"].startswith("missed:") and "dropped" in thu_sj["refit_note"]
    cur = plan["weeks"][1]["sessions"]
    assert cur[1]["session_type"] == "vo2max" and cur[1]["user_swapped"] is True   # Saturday untouched (pinned)
    assert cur[0]["session_type"] in ("z2", "recovery") and cur[0]["duration_min"] <= 60   # Friday stays easy, within the cap
    assert all(s["duration_min"] <= 60 for s in cur)                                # nothing past the 1-hour cap
    assert plan["weeks"][1]["sessions"][1]["duration_min"] == 60


def test_refit_note_round_trips_through_json_and_dict():
    import app as app_module
    s = _s(TODAY, "z2", 60, 45, refit_note="missed: 80 TSS dropped — no room")
    j = app_module._planned_session_to_json(s)
    assert j["refit_note"] == "missed: 80 TSS dropped — no room"
    assert app_module._planned_session_from_json(j).refit_note == s.refit_note
    assert tp._planned_session_from_dict(j, TODAY).refit_note == s.refit_note
