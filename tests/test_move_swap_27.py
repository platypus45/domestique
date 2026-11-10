"""#27 — dropping a workout on a training day swaps the two, never destroys.

A rider's drag used to overwrite whatever the target day held and leave a
rest stub behind: the target's workout was gone without a word. A rider's
move now trades the two sessions' places (both pinned where the rider put
them); a rest day, a vacated stub or an empty day is taken as before. A
ridden day or a day with two rows is refused with a plain sentence, and the
auto-reschedule keeps its overwrite (it writes off an easy day by design and
its undo relies on the stub).

Also covers the owner's O4 decision: a session the rider moved keeps its
length when the availability pass runs (a 0 h day still wins).

The clock is frozen wherever a status depends on today. Toast strings run the
real dashboard functions under node, as tests/test_drop_on_rest_days.py does.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app
import clock
import training_planner as tp

MON = date(2026, 10, 12)  # a Monday
RIDDEN = "That day has already been ridden."
TWO_ROWS = "Days with two sessions can't be changed by dragging yet."


def _d(i: int, start: date = MON) -> str:
    return (start + timedelta(days=i)).isoformat()


def _s(day: str, st: str, dur: int = 60, tss: int = 50, status: str = "pending", **kw) -> dict:
    d = date.fromisoformat(day)
    s = {"day": day, "day_name": d.strftime("%a"), "session_type": st,
         "duration_min": 0 if st == "rest" else dur,
         "tss_estimate": 0 if st == "rest" else tss,
         "description": st, "zwo_file": "" if st == "rest" else f"{st}.zwo",
         "zwo_name": "" if st == "rest" else f"{st} workout", "status": status}
    s.update(kw)
    return s


def _week(types, start: date = MON, statuses=None, **extra) -> dict:
    statuses = statuses or {}
    sessions = [_s(_d(i, start), t, status=statuses.get(i, "pending"))
                for i, t in enumerate(types)]
    plan = {"goal": {"rest_days": [6], "available_days": [0, 1, 2, 3, 4, 5]},
            "availability": {},
            "weeks": [{"week_num": 1, "start": start.isoformat(), "end": _d(6, start),
                       "phase": "base", "tss_target": 0, "is_stepback": False,
                       "sessions": sessions}]}
    plan.update(extra)
    return plan


def _on(plan: dict) -> dict:
    return {s["day"]: s for w in plan["weeks"] for s in w["sessions"]}


def _stubs(plan: dict) -> int:
    return sum(1 for w in plan["weeks"] for s in w["sessions"]
               if str(s.get("status") or "").startswith("moved_from:"))


@pytest.fixture
def frozen():
    yield clock.freeze
    clock.unfreeze()


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    path = tmp_path / "current_plan.json"
    client = TestClient(app.app)

    def write(plan):
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path.read_bytes()

    def post(src, dst):
        return client.post("/api/plan/move-session", json={"date": src, "new_date": dst})

    return SimpleNamespace(path=path, client=client, write=write, post=post,
                           read=lambda: json.loads(path.read_text(encoding="utf-8")))


# ── A1-A5: the swap itself ──────────────────────────────────────────────────

def test_a_drop_on_a_training_day_swaps_both_sessions(api, frozen):
    frozen(MON)
    plan = _week(["rest", "z2", "sweetspot", "vo2max", "rest", "long_z2", "rest"])
    before = _on(plan)
    stubs_before = _stubs(plan)
    api.write(plan)

    r = api.post(_d(1), _d(2))
    assert r.status_code == 200, r.text
    after = api.read()
    on = _on(after)

    # A1: both placed, each day once in its stored week.
    assert (on[_d(2)]["session_type"], on[_d(2)]["zwo_file"]) == ("z2", "z2.zwo")
    assert (on[_d(1)]["session_type"], on[_d(1)]["zwo_file"]) == ("sweetspot", "sweetspot.zwo")
    days = [s["day"] for s in after["weeks"][0]["sessions"]]
    assert days.count(_d(1)) == 1 and days.count(_d(2)) == 1
    # A2: both pinned, each moved_from the day it left, day fields follow.
    for day, left in ((_d(2), _d(1)), (_d(1), _d(2))):
        s = on[day]
        assert s["user_moved"] is True and s["auto_moved"] is False
        assert s["moved_from"] == left
        assert s["day"] == day and s["day_name"] == date.fromisoformat(day).strftime("%a")
        assert s["status"] == "pending"
    # A3: no stub written.
    assert _stubs(after) == stubs_before
    # A4: write-time load-neutral over the ISO week.
    assert sum(s["tss_estimate"] for s in on.values()) == sum(s["tss_estimate"] for s in before.values())
    assert sum(s["duration_min"] for s in on.values()) == sum(s["duration_min"] for s in before.values())
    assert Counter((s["session_type"], s["zwo_file"]) for s in on.values()) == \
        Counter((s["session_type"], s["zwo_file"]) for s in before.values())


def test_the_reverse_drag_restores_both_days(api, frozen):
    frozen(MON)
    plan = _week(["rest", "z2", "sweetspot", "vo2max", "rest", "long_z2", "rest"])
    before = _on(plan)
    api.write(plan)
    assert api.post(_d(1), _d(2)).status_code == 200
    r = api.post(_d(2), _d(1))
    assert r.status_code == 200, r.text
    assert r.json()["swapped"]["session_type"] == "sweetspot"   # now back on Wednesday
    on = _on(api.read())
    for day in (_d(1), _d(2)):
        for k in ("session_type", "zwo_file", "duration_min", "tss_estimate", "status"):
            assert on[day][k] == before[day][k], (day, k)


def test_a_swap_response_names_the_displaced_session_and_vacates_nothing(api, frozen):
    frozen(MON)
    api.write(_week(["rest", "z2", "sweetspot", "vo2max", "rest", "long_z2", "rest"]))
    r = api.post(_d(1), _d(3))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["vacated"] is None
    assert body["moved"]["session_type"] == "z2" and body["moved"]["day"] == _d(3)
    assert body["swapped"]["session_type"] == "vo2max" and body["swapped"]["day"] == _d(1)
    # A12: no side channel anywhere, response or stored plan.
    assert "_swapped" not in json.dumps(body)
    assert "_swapped" not in api.path.read_text(encoding="utf-8")


# ── A6: one landing rule for both sessions ──────────────────────────────────

def test_a_missed_partner_is_owed_on_a_day_still_to_come_and_the_drag_is_symmetric(frozen):
    frozen(MON + timedelta(days=2))  # Wednesday
    statuses = {0: "missed", 1: "missed"}
    types = ["vo2max", "z2", "rest", "z2", "rest", "rest", "rest"]

    p = _week(types, statuses=statuses)
    assert app._apply_move_session(p, _d(3), _d(0)) is not None    # Thu z2 onto Mon's missed VO2
    on = _on(p)
    assert (on[_d(0)]["session_type"], on[_d(0)]["status"]) == ("z2", "pending")
    assert (on[_d(3)]["session_type"], on[_d(3)]["status"]) == ("vo2max", "pending")

    q = _week(types, statuses=statuses)
    assert app._apply_move_session(q, _d(0), _d(3)) is not None    # the other way round
    assert {k: (v["session_type"], v["status"]) for k, v in _on(q).items()} == \
        {k: (v["session_type"], v["status"]) for k, v in on.items()}


def test_a_missed_session_landing_on_a_past_day_stays_missed(frozen):
    frozen(MON + timedelta(days=2))  # Wednesday
    p = _week(["vo2max", "z2", "rest", "z2", "rest", "rest", "rest"],
              statuses={0: "missed", 1: "missed"})
    assert app._apply_move_session(p, _d(0), _d(1)) is not None
    on = _on(p)
    assert (on[_d(1)]["session_type"], on[_d(1)]["status"]) == ("vo2max", "missed")
    assert (on[_d(0)]["session_type"], on[_d(0)]["status"]) == ("z2", "missed")


def test_a_dismissed_partner_travels_dismissed(api, frozen):
    frozen(MON)
    plan = _week(["rest", "z2", "sweetspot", "rest", "rest", "rest", "rest"],
                 statuses={2: "dismissed"})
    plan["weeks"][0]["sessions"][2]["dismissed_at"] = "2026-10-11T08:00:00"
    api.write(plan)
    r = api.post(_d(1), _d(2))
    assert r.status_code == 200, r.text
    on = _on(api.read())
    assert on[_d(1)]["session_type"] == "sweetspot"
    assert on[_d(1)]["status"] == "dismissed"
    assert on[_d(1)]["dismissed_at"] == "2026-10-11T08:00:00"
    assert (on[_d(2)]["session_type"], on[_d(2)]["status"]) == ("z2", "pending")


# ── A7-A9: refusals ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("side,status", [
    ("dst", "done"), ("dst", "done_partial"), ("dst", "ambiguous"),
    ("src", "done"), ("src", "done_partial"), ("src", "ambiguous"),
])
def test_a_ridden_day_is_refused_and_nothing_changes(api, frozen, side, status):
    frozen(MON + timedelta(days=1))
    idx = 1 if side == "src" else 2
    plan = _week(["rest", "z2", "sweetspot", "rest", "rest", "rest", "rest"],
                 statuses={idx: status})
    plan["weeks"][0]["sessions"][idx]["completion_matches"] = [{"activity_id": 9}]
    raw = api.write(plan)

    r = api.post(_d(1), _d(2))
    assert r.status_code == 409
    assert r.json() == {"error": RIDDEN}
    assert api.path.read_bytes() == raw

    p = copy.deepcopy(plan)
    assert app._apply_move_session(p, _d(1), _d(2)) is None
    assert p == plan


@pytest.mark.parametrize("two_row_day", [1, 2])
def test_a_day_with_two_rows_is_refused_and_nothing_changes(api, frozen, two_row_day):
    frozen(MON)
    plan = _week(["rest", "z2", "sweetspot", "rest", "rest", "rest", "rest"])
    plan["weeks"][0]["sessions"].insert(two_row_day + 1, _s(_d(two_row_day), "threshold"))
    raw = api.write(plan)

    r = api.post(_d(1), _d(2))
    assert r.status_code == 409
    assert r.json() == {"error": TWO_ROWS}
    assert api.path.read_bytes() == raw

    p = copy.deepcopy(plan)
    assert app._apply_move_session(p, _d(1), _d(2)) is None
    assert p == plan


@pytest.mark.parametrize("race_day", [1, 2])
def test_a_race_day_is_still_refused_with_400(api, frozen, race_day):
    frozen(MON)
    plan = _week(["rest", "z2", "sweetspot", "rest", "rest", "rest", "rest"])
    plan["weeks"][0]["sessions"][race_day]["is_race"] = True
    raw = api.write(plan)
    r = api.post(_d(1), _d(2))
    assert r.status_code == 400
    assert r.json() == {"error": "race day is fixed — edit the race instead"}
    assert api.path.read_bytes() == raw


# ── A10: the auto path keeps its overwrite ──────────────────────────────────

def test_the_auto_reschedule_still_takes_over_the_easy_day():
    p = _week(["vo2max", "z2", "z2", "z2", "z2", "z2", "rest"], statuses={0: "missed"})
    moved = app._apply_move_session(p, _d(0), _d(3), reset_status_to_pending=True, auto=True)
    assert moved is not None and "_swapped" not in moved
    on = _on(p)
    assert on[_d(3)]["session_type"] == "vo2max"
    assert on[_d(3)]["auto_moved"] is True and on[_d(3)]["user_moved"] is False
    assert on[_d(0)]["status"] == f"moved_from:{_d(3)}"
    assert p["auto_moves"][-1]["displaced"]["session_type"] == "z2"
    assert sum(1 for s in on.values() if s["session_type"] == "z2") == 4


def test_the_auto_reschedule_never_takes_a_day_the_rider_swapped():
    today = MON + timedelta(days=1)  # Tuesday
    types = ["vo2max", "z2", "recovery", "z2", "z2", "long_z2", "rest"]
    ctrl = _week(types, statuses={0: "missed"})
    picked = [x["suggested_date"] for x in app._compute_missed_suggestions(ctrl, today)]
    assert picked and picked[0] in (_d(1), _d(2)), picked   # the control takes a day we will swap

    p = _week(types, statuses={0: "missed"})
    clock.freeze(today)
    try:
        assert app._apply_move_session(p, _d(1), _d(2)) is not None
    finally:
        clock.unfreeze()
    picked = [x["suggested_date"] for x in app._compute_missed_suggestions(p, today)]
    assert not {_d(1), _d(2)} & set(picked), picked


def test_a_rider_swap_of_an_auto_moved_session_drops_its_undo_record():
    p = _week(["vo2max", "z2", "z2", "z2", "z2", "z2", "rest"], statuses={0: "missed"})
    today = MON + timedelta(days=1)
    assert app._apply_move_session(p, _d(0), _d(3), reset_status_to_pending=True, auto=True)
    assert len(p["auto_moves"]) == 1
    clock.freeze(today)
    try:
        assert app._apply_move_session(p, _d(3), _d(4)) is not None   # rider swaps Thu <-> Fri
        before = copy.deepcopy(_on(p))
        assert app._undo_auto_moves_for_ridden_days(p, today) == []
    finally:
        clock.unfreeze()
    assert p["auto_moves"] == []
    assert _on(p) == before


# ── A11: rest-like targets still move ───────────────────────────────────────

@pytest.mark.parametrize("target", ["rest", "stub", "empty"])
def test_a_rest_like_target_still_moves_and_vacates(api, frozen, target):
    frozen(MON)
    plan = _week(["rest", "z2", "sweetspot", "vo2max", "rest", "long_z2", "rest"])
    sessions = plan["weeks"][0]["sessions"]
    if target == "stub":
        sessions[4] = dict(sessions[4], status=f"moved_from:{_d(5)}", description=f"Moved to {_d(5)}")
    elif target == "empty":
        del sessions[4]
    api.write(plan)

    r = api.post(_d(3), _d(4))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vacated"] == _d(3) and body["swapped"] is None
    on = _on(api.read())
    assert on[_d(4)]["session_type"] == "vo2max" and on[_d(4)]["user_moved"] is True
    assert on[_d(3)]["session_type"] == "rest"
    assert on[_d(3)]["status"] == f"moved_from:{_d(4)}"


# ── W14: a rest day or a stub is not a session to drag ──────────────────────

@pytest.mark.parametrize("source", ["rest", "stub"])
def test_a_rest_day_or_a_stub_is_not_a_session_to_drag(api, frozen, source):
    frozen(MON)
    plan = _week(["rest", "z2", "sweetspot", "vo2max", "rest", "long_z2", "rest"])
    sessions = plan["weeks"][0]["sessions"]
    if source == "stub":
        sessions[4] = dict(sessions[4], status=f"moved_from:{_d(5)}", description=f"Moved to {_d(5)}")
    raw = api.write(plan)

    # Onto a training day (it would swap) and onto a rest day (it would move).
    for dst in (_d(3), _d(6)):
        r = api.post(_d(4), dst)
        assert r.status_code == 422, r.text
        assert r.json() == {"error": "source_session_not_found", "date": _d(4)}
        assert api.path.read_bytes() == raw

        p = copy.deepcopy(plan)
        assert app._apply_move_session(p, _d(4), dst) is None
        assert p == plan


# ── A13: a swap across two stored weeks of one ISO week ─────────────────────

def test_a_swap_across_fri_thu_stored_weeks(api, frozen):
    # Fri-Thu stored weeks; Tue 04-28 (first) <-> Fri 05-01 (second), ISO week 18.
    def wk(start, types):
        s = date.fromisoformat(start)
        return {"start": start, "end": (s + timedelta(days=6)).isoformat(),
                "sessions": [_s((s + timedelta(days=i)).isoformat(), t) for i, t in enumerate(types)]}
    plan = {"goal": {}, "weeks": [wk("2026-04-24", ["tempo", "z2", "rest", "rest", "z2", "rest", "z2"]),
                                  wk("2026-05-01", ["vo2max", "rest", "rest", "rest", "rest", "rest", "rest"])]}
    frozen(date(2026, 4, 27))
    api.write(plan)

    r = api.post("2026-04-28", "2026-05-01")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vacated"] is None and body["swapped"]["session_type"] == "vo2max"
    assert body["swapped"]["day"] == "2026-04-28"
    after = api.read()
    first = [s["day"] for s in after["weeks"][0]["sessions"]]
    second = [s["day"] for s in after["weeks"][1]["sessions"]]
    assert first.count("2026-04-28") == 1 and "2026-05-01" not in first
    assert second.count("2026-05-01") == 1 and "2026-04-28" not in second
    on = _on(after)
    assert on["2026-05-01"]["session_type"] == "z2" and on["2026-05-01"]["moved_from"] == "2026-04-28"
    assert on["2026-04-28"]["session_type"] == "vo2max" and on["2026-04-28"]["moved_from"] == "2026-05-01"


# ── A15: the endpoint holds the plan lock across its read and its write ────

# The endpoint's own frames: its locked read -> mutate -> write runs in a
# nested function on a worker thread (W15).
_ENDPOINT_FRAMES = ("api_plan_move_session", "_move_under_plan_lock")


def test_the_endpoint_reads_and_writes_inside_one_hold_of_the_plan_lock(api, frozen, monkeypatch):
    frozen(MON)
    api.write(_week(["rest", "z2", "sweetspot", "vo2max", "rest", "long_z2", "rest"]))
    real_lock, real_load, real_write = tp.plan_write_lock, json.load, tp.atomic_write_plan
    state = {"depth": 0, "hold": 0, "events": []}

    @contextmanager
    def recording_lock():
        with real_lock():
            if state["depth"] == 0:
                state["hold"] += 1
            state["depth"] += 1
            try:
                yield
            finally:
                state["depth"] -= 1

    def held():
        return state["hold"] if state["depth"] else None

    # Only the endpoint's own read and write count: the app's other plan
    # readers (the boot-time self-heal runs on a worker thread) are not the
    # read-modify-write under test.
    def rec_load(f, *a, **k):
        if sys._getframe(1).f_code.co_name in _ENDPOINT_FRAMES:
            state["events"].append(("read", held()))
        return real_load(f, *a, **k)

    def rec_write(path, plan):
        if sys._getframe(1).f_code.co_name in _ENDPOINT_FRAMES:
            state["events"].append(("write", held()))
        return real_write(path, plan)

    monkeypatch.setattr(tp, "plan_write_lock", recording_lock)
    monkeypatch.setattr(json, "load", rec_load)
    monkeypatch.setattr(tp, "atomic_write_plan", rec_write)

    assert api.post(_d(1), _d(2)).status_code == 200
    reads = [h for e, h in state["events"] if e == "read"]
    writes = [h for e, h in state["events"] if e == "write"]
    assert reads and writes, state["events"]
    assert None not in reads + writes, state["events"]
    assert set(reads) == set(writes) and len(set(writes)) == 1, state["events"]


# ── W15: the move waits for the plan lock off the event loop ───────────────

def test_the_move_waits_for_the_plan_lock_off_the_event_loop(api, frozen, monkeypatch):
    # A background adapt can hold the plan lock for a whole plan update. The
    # move's locked read/write must wait on a worker thread, not on the event
    # loop thread that serves every other request.
    frozen(MON)
    api.write(_week(["rest", "z2", "sweetspot", "vo2max", "rest", "long_z2", "rest"]))
    real_body, real_load = app._get_json_body, json.load
    seen = {"loop": None, "locked": []}

    async def rec_body(request):
        seen["loop"] = threading.get_ident()      # body parsing stays on the loop
        return await real_body(request)

    def rec_load(f, *a, **k):
        if sys._getframe(1).f_code.co_name in _ENDPOINT_FRAMES:
            seen["locked"].append(threading.get_ident())
        return real_load(f, *a, **k)

    monkeypatch.setattr(app, "_get_json_body", rec_body)
    monkeypatch.setattr(json, "load", rec_load)

    assert api.post(_d(1), _d(2)).status_code == 200
    assert seen["loop"] is not None and seen["locked"], seen
    assert seen["loop"] not in seen["locked"], seen


# ── D1 (owner O4): a rider-moved session keeps its length ──────────────────

def _avail_week(hours) -> dict:
    return {_d(i): {"type": "available" if h > 0 else "unavailable", "hours": h}
            for i, h in enumerate(hours)}


def _adapt(plan) -> None:
    # What _apply_plan_update hands reforecast on every ride sync: the whole
    # densely materialised availability calendar.
    overrides = {k: float(v["hours"]) for k, v in plan["availability"].items() if "hours" in v}
    tp.reforecast_dict(plan, today_iso=_d(0), availability_overrides=overrides)


def _long_ride_week(hours) -> dict:
    p = _week(["rest", "vo2max", "z2", "rest", "z2", "long_z2", "rest"],
              availability=_avail_week(hours))
    p["weeks"][0]["sessions"][5].update(duration_min=180, tss_estimate=140)
    return p


def test_a_long_ride_swapped_onto_a_short_day_keeps_its_length(frozen):
    frozen(MON)
    p = _long_ride_week([1.0, 1.0, 1.0, 1.0, 1.0, 3.0, 0.0])
    assert app._apply_move_session(p, _d(1), _d(5)) is not None   # Tue VO2 <-> Sat long ride
    _adapt(p)
    tue = _on(p)[_d(1)]
    assert (tue["session_type"], tue["duration_min"], tue["tss_estimate"], tue["zwo_file"]) == \
        ("long_z2", 180, 140, "long_z2.zwo")
    assert tue["user_moved"] is True


def test_a_long_ride_moved_onto_a_short_rest_day_keeps_its_length(frozen):
    frozen(MON)
    p = _long_ride_week([1.0, 1.0, 1.0, 1.0, 1.0, 3.0, 0.0])
    assert app._apply_move_session(p, _d(5), _d(3)) is not None   # Sat -> Thu (rest)
    _adapt(p)
    thu = _on(p)[_d(3)]
    assert (thu["session_type"], thu["duration_min"], thu["zwo_file"]) == ("long_z2", 180, "long_z2.zwo")


def test_an_unavailable_day_still_rests_a_moved_session_as_it_does_a_swapped_one(frozen):
    frozen(MON)
    p = _long_ride_week([1.0, 0.0, 1.0, 1.0, 1.0, 3.0, 0.0])
    p["weeks"][0]["sessions"][2]["user_swapped"] = True           # Wed: the user_swapped twin
    p["availability"][_d(2)] = {"type": "unavailable", "hours": 0.0}
    assert app._apply_move_session(p, _d(5), _d(1)) is not None   # long ride onto 0 h Tue
    _adapt(p)
    on = _on(p)
    for day in (_d(1), _d(2)):
        assert (on[day]["session_type"], on[day]["duration_min"], on[day]["zwo_file"]) == ("rest", 0, ""), day


def test_a_session_nobody_moved_is_still_sized_to_its_day(frozen):
    frozen(MON)
    p = _long_ride_week([1.0, 1.0, 1.0, 1.0, 1.0, 3.0, 0.0])
    p["weeks"][0]["sessions"][2].update(duration_min=120, tss_estimate=90)   # Wed z2 on a 1 h day
    assert app._apply_move_session(p, _d(1), _d(5)) is not None
    _adapt(p)
    assert _on(p)[_d(2)]["duration_min"] == 60


# ── A16: toast text (node) ──────────────────────────────────────────────────

SRC = (Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html"
       ).read_text(encoding="utf-8")


def _fn(name: str) -> str:
    i = SRC.index(f"function {name}(")
    if SRC[i - 6:i] == "async ":
        i -= 6
    depth = 0
    for j in range(SRC.index("{", i), len(SRC)):
        if SRC[j] == "{":
            depth += 1
        elif SRC[j] == "}":
            depth -= 1
            if depth == 0:
                return SRC[i:j + 1]
    raise AssertionError(name)


def _node(js: str):
    res = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_the_toast_helper_says_where_each_session_went():
    out = _node(_fn("_moveToastText") + """
console.log(JSON.stringify([
  _moveToastText({ok: true, swapped: {zwo_name: 'server B'}}, 'A', 'B', 'Tuesday', 'Wednesday'),
  _moveToastText({ok: true, swapped: null, vacated: '2026-10-13'}, 'A', 'B', 'Tuesday', 'Wednesday'),
  _moveToastText({ok: true, swapped: {zwo_name: 'server B'}}, 'A', '', 'Tuesday', 'Wednesday'),
  _moveToastText({ok: true, swapped: {session_type: 'vo2max'}}, 'A', '', 'Tuesday', 'Wednesday'),
]));
""")
    assert out == ["Moved A to Wednesday and B to Tuesday",
                   "Moved A from Tuesday to Wednesday",
                   "Moved A to Wednesday and server B to Tuesday",
                   "Moved A to Wednesday and VO2MAX to Tuesday"]
    assert not any("swap" in t.lower() for t in out)


def test_the_toast_helper_names_a_skipped_partner_as_skipped():
    # W12: a dismissed partner travels dismissed; nothing goes to the watch for
    # it, so the toast must not read as if it were back on.
    out = _node(_fn("_moveToastText") + """
console.log(JSON.stringify([
  _moveToastText({ok: true, swapped: {zwo_name: 'server B', status: 'dismissed'}}, 'A', 'B', 'Tuesday', 'Wednesday'),
  _moveToastText({ok: true, swapped: {session_type: 'z2', status: 'dismissed'}}, 'A', '', 'Tue', 'Wed'),
  _moveToastText({ok: true, swapped: {zwo_name: 'server B', status: 'missed'}}, 'A', 'B', 'Tuesday', 'Wednesday'),
]));
""")
    assert out == ["Moved A to Wednesday; B (skipped) is now on Tuesday",
                   "Moved A to Wed; Z2 (skipped) is now on Tue",
                   "Moved A to Wednesday and B to Tuesday"]


def test_the_toast_helper_never_shows_a_raw_code():
    # W11: the structured 422 codes read as a sentence; a refusal's own
    # sentence is shown; anything else is a plain failure.
    out = _node(_fn("_moveToastText") + """
console.log(JSON.stringify([
  _moveToastText({error: 'source_session_not_found', date: '2026-10-13'}, 'A', 'B', 'Tue', 'Wed'),
  _moveToastText({error: 'target_week_not_found', date: '2026-12-03'}, 'A', 'B', 'Tue', 'Wed'),
  _moveToastText({error: 'That day has already been ridden.'}, 'A', 'B', 'Tue', 'Wed'),
  _moveToastText({detail: 'Move failed'}, 'A', 'B', 'Tue', 'Wed'),
  _moveToastText({}, 'A', 'B', 'Tue', 'Wed'),
]));
""")
    assert out == ["Couldn't find that session. Try refreshing.",
                   "That day is outside your plan.",
                   "Move failed: That day has already been ridden.",
                   "Move failed.",
                   "Move failed."]


_DROP_ENV = """
global.HTMLElement = Object;
const toasts = [];
global.showToast = (m) => toasts.push(m);
global.document = { querySelectorAll: () => [] };
global.loadCalendar = async () => {};
global.loadPlan = async () => {};
function respond(status, body) {
  global.fetch = async () => ({ ok: status < 300, status, json: async () => body });
}
function ev(src, validClass) {
  return { preventDefault() {}, stopPropagation() {},
           currentTarget: { classList: { contains: c => c === validClass } },
           dataTransfer: { getData: () => src } };
}
"""


def test_the_calendar_drop_names_both_sessions_and_shows_a_refusal():
    js = _DROP_ENV + _fn("calDragEnd") + "\n" + _fn("_moveToastText") + "\n" + _fn("calDrop") + """
global.window = { _calData: { weeks: [ { days: [
  { date: '2026-10-13', planned: { name: 'Endurance 60' } },
  { date: '2026-10-14', planned: { name: 'Sweet Spot 2x20' } },
] } ] } };
(async () => {
  respond(200, { ok: true, moved: {}, vacated: null, swapped: { zwo_name: 'ss_2x20' } });
  await calDrop(ev('2026-10-13', 'cal-dropping-valid'), '2026-10-14', 0);
  respond(200, { ok: true, moved: {}, vacated: '2026-10-13', swapped: null });
  await calDrop(ev('2026-10-13', 'cal-dropping-valid'), '2026-10-14', 0);
  respond(409, { error: 'That day has already been ridden.' });
  await calDrop(ev('2026-10-13', 'cal-dropping-valid'), '2026-10-14', 0);
  respond(500, { detail: 'Move failed' });
  await calDrop(ev('2026-10-13', 'cal-dropping-valid'), '2026-10-14', 0);
  respond(422, { error: 'target_week_not_found', date: '2026-10-14' });
  await calDrop(ev('2026-10-13', 'cal-dropping-valid'), '2026-10-14', 0);
  respond(422, { error: 'source_session_not_found', date: '2026-10-13' });
  await calDrop(ev('2026-10-13', 'cal-dropping-valid'), '2026-10-14', 0);
  respond(200, { ok: true, moved: {}, vacated: null, swapped: { zwo_name: 'ss_2x20', status: 'dismissed' } });
  await calDrop(ev('2026-10-13', 'cal-dropping-valid'), '2026-10-14', 0);
  console.log(JSON.stringify(toasts));
})();
"""
    assert _node(js) == [
        "Moved Endurance 60 to Wednesday and Sweet Spot 2x20 to Tuesday",
        "Moved Endurance 60 from Tuesday to Wednesday",
        "Move failed: That day has already been ridden.",
        "Move failed.",
        "That day is outside your plan.",
        "Couldn't find that session. Try refreshing.",
        "Moved Endurance 60 to Wednesday; Sweet Spot 2x20 (skipped) is now on Tuesday",
    ]


def test_the_plan_grid_drop_names_both_sessions():
    js = _DROP_ENV + _fn("pgDragEnd") + "\n" + _fn("_moveToastText") + "\n" + _fn("pgDrop") + """
global.window = { _planData: { weeks: [ { sessions: [
  { day: '2026-10-13', day_name: 'Tue', zwo_name: 'Endurance 60', session_type: 'z2' },
  { day: '2026-10-14', day_name: 'Wed', zwo_name: '', session_type: 'sweetspot' },
] } ] } };
(async () => {
  respond(200, { ok: true, moved: {}, vacated: null, swapped: { zwo_name: 'server name' } });
  await pgDrop(ev('2026-10-13', 'pg-dropping-valid'), '2026-10-14', 0);
  respond(409, { error: "Days with two sessions can't be changed by dragging yet." });
  await pgDrop(ev('2026-10-13', 'pg-dropping-valid'), '2026-10-14', 0);
  respond(422, { error: 'target_week_not_found', date: '2026-10-14' });
  await pgDrop(ev('2026-10-13', 'pg-dropping-valid'), '2026-10-14', 0);
  respond(422, { error: 'source_session_not_found', date: '2026-10-13' });
  await pgDrop(ev('2026-10-13', 'pg-dropping-valid'), '2026-10-14', 0);
  respond(500, { detail: 'Move failed' });
  await pgDrop(ev('2026-10-13', 'pg-dropping-valid'), '2026-10-14', 0);
  respond(200, { ok: true, moved: {}, vacated: null, swapped: { zwo_name: 'server name', status: 'dismissed' } });
  await pgDrop(ev('2026-10-13', 'pg-dropping-valid'), '2026-10-14', 0);
  console.log(JSON.stringify(toasts));
})();
"""
    assert _node(js) == [
        "Moved Endurance 60 to Wed and SWEETSPOT to Tue",
        "Move failed: Days with two sessions can't be changed by dragging yet.",
        "That day is outside your plan.",
        "Couldn't find that session. Try refreshing.",
        "Move failed.",
        "Moved Endurance 60 to Wed; SWEETSPOT (skipped) is now on Tue",
    ]


# ── W16: a "busy" debounced calendar push is re-armed once ──────────────────

def test_a_busy_debounced_calendar_push_is_re_armed_once(monkeypatch):
    import icu_calendar_push as icp
    from profile_manager import ProfileManager

    monkeypatch.setattr(app, "_icu_push_sync_enabled", lambda: True)
    monkeypatch.setattr(ProfileManager, "get",
                        staticmethod(lambda: SimpleNamespace(active_id="pid-w16")))
    monkeypatch.setattr(app, "_icu_push_last_result", None)
    monkeypatch.setattr(app, "_icu_push_timer", None)
    result = {"res": {"error": "busy"}}
    monkeypatch.setattr(icp, "reconcile", lambda **_k: result["res"])

    def armed():
        t = app._icu_push_timer
        if t is not None:
            t.cancel()
            app._icu_push_timer = None
        return t

    try:
        app._icu_push_debounced_run("pid-w16")
        t = armed()
        assert t is not None and t.args == ("pid-w16", 1)     # re-armed, as retry 1

        app._icu_push_debounced_run("pid-w16", 1)              # the retry is busy too
        t = armed()
        assert t is not None and t.args == ("pid-w16", 2)     # 3.13.0: keeps trying

        app._icu_push_debounced_run("pid-w16", 10)             # but not forever
        assert armed() is None

        result["res"] = {"pushed": 1, "updated": 0, "deleted": 0}
        app._icu_push_debounced_run("pid-w16")
        assert armed() is None                                  # only "busy" re-arms

        # A newer plan write armed its own timer while this run waited: keep it.
        newer = SimpleNamespace(cancel=lambda: None)

        def busy_after_a_new_write(**_k):
            app._icu_push_timer = newer
            return {"error": "busy"}

        monkeypatch.setattr(icp, "reconcile", busy_after_a_new_write)
        app._icu_push_debounced_run("pid-w16")
        assert app._icu_push_timer is newer
    finally:
        armed()
