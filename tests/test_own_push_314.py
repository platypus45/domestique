"""3.14.0 — the rider's own rides in the intervals.icu calendar (owner O3,
O4; MASTER C1-C7; grill_sessions H4, H5, M14, L10; grill_integration X2,
X3b, X5).

A ride the rider added in Domestique goes out as a plain planned ride under
``domestique:<pid>:<day>:own``, in a POST of its own after the workouts; a
ride imported from intervals.icu never does. A rider move of an own event
is held and reported as own_moved until the rider chooses in the ride's
dialog. Plans hold own rows in the shape of the MASTER §3 contract, written
by hand (F2 owns the writer). Hermetic like tests/test_icu_hold_28.py.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import clock
import icu_calendar_push as icp
import training_planner as tp
from test_icu_push import stub, _sess, _write_plan, _apikey, _eid, STEADY_ZWO, TEMPO_ZWO  # noqa: F401
from test_icu_hold_28 import WindowedFakeICU, MON, D, _week, _rest, _at, _counts

OWN_KEYS = {"start_date_local", "category", "type", "name", "external_id",
            "moving_time", "icu_training_load", "description"}


@pytest.fixture()
def env(stub, freeze_clock, monkeypatch):
    freeze_clock(MON)
    _apikey(stub)
    (stub.workouts / "sweet.zwo").write_text(TEMPO_ZWO, encoding="utf-8")
    (stub.workouts / "vo2.zwo").write_text(STEADY_ZWO, encoding="utf-8")
    stub.fk = WindowedFakeICU()
    monkeypatch.setattr(icp, "_http", stub.fk)
    icp._RECENT_PUSH.clear()
    yield stub
    icp._RECENT_PUSH.clear()


class RejectsFileless(WindowedFakeICU):
    """intervals.icu refusing the whole batch when it holds an event without
    a workout file (the owner's live check decides whether it does)."""

    def __call__(self, method, path, payload=None, timeout=30.0):
        if method == "POST" and any("file_contents_base64" not in ev for ev in payload):
            self.calls.append((method, path, payload))
            return 422, b'{"error":"moving_time is not allowed here"}'
        return super().__call__(method, path, payload, timeout)


def _own_row(day, name="Saturday club ride", intensity="hard", minutes=180,
             tss=240.0, source="manual", icu_event_id=None, replaced=None,
             own_extra=None, **kw):
    """An own row as F2 writes it (MASTER §3)."""
    s = _sess(day, zwo="", stype={"easy": "z2", "moderate": "tempo",
                                  "hard": "sweetspot"}[intensity],
              duration_min=minutes, tss_estimate=tss, description=name)
    s.update(zwo_name="", user_moved=False, user_swapped=False, adapted=False,
             moved_from="", completion_matches=None,
             own={"name": name, "intensity": intensity, "load_from": "intensity",
                  "source": source, "icu_event_id": icu_event_id,
                  "icu_day": day if source == "icu" else None,
                  "replaced": replaced, "added_at": "2026-10-19T08:00:00",
                  **(own_extra or {})})
    s.update(kw)
    return s


def _own_id(env, day):
    return f"domestique:{env.pid}:{day}:own"


def _run(env):
    r = icp.reconcile()
    assert not r.get("error") and not r.get("needs_reconnect"), r
    return r


def _seed(env, ss):
    _write_plan(ss)
    r = _run(env)
    assert r["held"] == 0 and r["own_moved"] == []
    icp._RECENT_PUSH.clear()                            # the rider acts later
    return r


def _touched(env, since=0):
    posted, deleted = set(), set()
    for method, _path, payload in env.fk.calls[since:]:
        if method == "POST":
            posted |= {ev["external_id"] for ev in payload}
        elif method == "PUT":
            deleted |= {r.get("external_id") for r in payload}
    return posted, deleted


def _posts(env, since=0):
    return [c[2] for c in env.fk.calls[since:] if c[0] == "POST"]


def _plan():
    return json.loads((Path(tp.PLAN_DIR) / "current_plan.json").read_text())


def _with_own_saturday(env, **kw):
    """Mon sweet + Sat VO2 pushed; then the rider's club ride replaces the
    VO2 on Saturday (in Domestique)."""
    ss = _week(mon="sweet.zwo", sat="vo2.zwo")
    _seed(env, ss)
    i = _at(ss, D["sat"])
    ss[i] = _own_row(D["sat"], replaced=dict(ss[i]), **kw)
    _write_plan(ss)
    return ss


# ── C1 / O3: a plain planned ride under its own id ──────────────────────────

def test_an_own_ride_goes_out_as_a_plain_planned_ride_under_its_own_id(env):
    _with_own_saturday(env)
    mark = len(env.fk.calls)
    r = _run(env)
    workouts, own = _posts(env, mark)                   # two POSTs, workouts first
    assert [ev["external_id"] for ev in own] == [_own_id(env, D["sat"])]
    assert _own_id(env, D["sat"]) not in {ev["external_id"] for ev in workouts}
    ev = own[0]
    assert set(ev) == OWN_KEYS                          # no filename, no file
    assert (ev["start_date_local"], ev["category"], ev["type"], ev["name"]) == (
        f"{D['sat']}T00:00:00", "WORKOUT", "Ride", "Saturday club ride")
    assert (ev["moving_time"], ev["icu_training_load"]) == (180 * 60, 240)
    _posted, deleted = _touched(env, mark)
    assert deleted == {_eid(env, D["sat"])}             # the VO2 it replaced goes
    assert _counts(r) == (1, 0, 1) and r["skipped"] == []
    assert env.fk.where(_own_id(env, D["sat"])) == [D["sat"]]
    assert _counts(_run(env)) == (0, 0, 0)


def test_an_own_ride_without_a_length_is_skipped_and_its_slot_still_swept(env):
    _with_own_saturday(env, minutes=0)
    mark = len(env.fk.calls)
    r = _run(env)
    assert r["skipped"] == [{"day": D["sat"], "reason": "own_invalid"}]
    posted, deleted = _touched(env, mark)
    assert _eid(env, D["sat"]) in deleted              # never spared as "broken"
    assert not any(e.endswith(":own") for e in posted)


def test_an_edit_of_the_own_rides_length_counts_as_updated(env):
    ss = _with_own_saturday(env)
    _run(env)
    ss[_at(ss, D["sat"])] = _own_row(D["sat"], minutes=150, tss=200.0)
    _write_plan(ss)
    r = _run(env)
    assert _counts(r) == (0, 1, 0)
    ev = env.fk._one(_own_id(env, D["sat"]))
    assert (ev["moving_time"], ev["icu_training_load"]) == (150 * 60, 200)


def test_a_filename_on_our_own_ride_never_breaks_the_run(env):
    """X3b: every reader takes the filename with .get."""
    _with_own_saturday(env)
    _run(env)
    env.fk._one(_own_id(env, D["sat"]))["filename"] = "club.zwo"   # echoed somehow
    assert _counts(_run(env)) == (0, 0, 0)


# ── C2 / X2: own rides in a POST of their own ───────────────────────────────

def test_a_rejected_own_ride_never_keeps_the_workouts_off_the_watch(env, monkeypatch):
    env.fk = RejectsFileless()
    monkeypatch.setattr(icp, "_http", env.fk)
    ss = _with_own_saturday(env)
    ss[_at(ss, D["wed"])] = _sess(D["wed"], zwo="steady.zwo")   # a new workout too
    _write_plan(ss)
    r = icp.reconcile()
    assert not r.get("error"), r
    assert r["skipped"] == [{"day": D["sat"], "reason": "own_rejected"}]
    assert env.fk.files_on() == {D["mon"]: ["sweet"], D["wed"]: ["steady"]}


def test_a_rejected_own_ride_keeps_its_earlier_event(env, monkeypatch):
    ss = _with_own_saturday(env)
    _run(env)                                           # the ride is out
    rejecting = RejectsFileless(events=env.fk.events)
    rejecting._next_id = env.fk._next_id
    env.fk = rejecting
    monkeypatch.setattr(icp, "_http", rejecting)
    ss[_at(ss, D["sat"])] = _own_row(D["sat"], minutes=150)
    _write_plan(ss)
    mark = len(env.fk.calls)
    r = icp.reconcile()
    assert not r.get("error") and {"day": D["sat"], "reason": "own_rejected"} in r["skipped"]
    _posted, deleted = _touched(env, mark)
    assert _own_id(env, D["sat"]) not in deleted
    assert env.fk.where(_own_id(env, D["sat"])) == [D["sat"]]


# ── C5: a ride imported from intervals.icu is never pushed back ─────────────

@pytest.mark.parametrize("source", ["icu", "manual"])
def test_an_imported_own_ride_is_never_pushed_and_its_slot_leaves_the_watch(env, source):
    """source "manual" with an intervals.icu event id: a lost source must
    never turn the rider's ride into a duplicate."""
    ss = _week(mon="sweet.zwo", sat="vo2.zwo")
    _seed(env, ss)
    rider = {"id": 900, "category": "WORKOUT", "type": "Ride", "name": "Club ride",
             "start_date_local": f"{D['sat']}T00:00:00"}
    env.fk.events.append(dict(rider))
    ss[_at(ss, D["sat"])] = _own_row(D["sat"], name="Club ride", source=source,
                                     icu_event_id=900)
    _write_plan(ss)
    mark = len(env.fk.calls)
    r = _run(env)
    posted, deleted = _touched(env, mark)
    assert not any(e.endswith(":own") for e in posted) and r["skipped"] == []
    assert deleted == {_eid(env, D["sat"])}
    icp.sweep_all()
    for _m, _p, payload in env.fk.calls[mark:]:
        assert all(row.get("id") != 900 for row in payload or [] if isinstance(row, dict))
    assert any(e.get("id") == 900 for e in env.fk.events)


# ── C3 / O4 / X5: a rider move of an own event is held and reported ────────

def test_a_drag_of_our_own_ride_in_intervals_icu_is_held_and_reported(env):
    ss = _with_own_saturday(env)
    _run(env)
    own = _own_id(env, D["sat"])
    env.fk.drag(own, D["sun"])
    for _ in range(2):
        mark = len(env.fk.calls)
        r = _run(env)
        posted, deleted = _touched(env, mark)
        assert own not in posted | deleted and env.fk.where(own) == [D["sun"]]
        assert r["held"] == r["held_rides"] == 1
        assert r["own_moved"] == [{"day": D["sat"], "icu_day": D["sun"]}]
    # The ride's dialog reads it from the status after the rider's Push.
    assert env.client.post("/api/icu/push", json={}).json()["ok"]
    status = env.client.get("/api/icu/push").json()
    assert status["own_moved"] == [{"day": D["sat"], "icu_day": D["sun"]}]
    # O8: removing the ride in Domestique removes its event, wherever the
    # rider put it (restated in Wave 4: it used to stay held for good).
    ss[_at(ss, D["sat"])] = _rest(D["sat"])
    _write_plan(ss)
    mark = len(env.fk.calls)
    r = _run(env)
    _posted, deleted = _touched(env, mark)
    assert own in deleted and env.fk.where(own) == []
    assert r["held"] == 0 and r["own_moved"] == []


def test_a_ride_since_linked_to_the_riders_own_event_leaves_only_their_event(env):
    """Our event of the manual ride was moved, then the ride was linked to
    the rider's intervals.icu event (M8): ours goes (O8), theirs stays."""
    ss = _with_own_saturday(env)
    _run(env)
    own = _own_id(env, D["sat"])
    env.fk.drag(own, D["sun"])
    env.fk.events.append({"id": 900, "category": "WORKOUT", "type": "Ride",
                          "name": "Club ride", "start_date_local": f"{D['sat']}T00:00:00"})
    ss[_at(ss, D["sat"])] = _own_row(D["sat"], source="icu", icu_event_id=900)
    _write_plan(ss)
    r = _run(env)
    assert r["own_moved"] == [] and r["held"] == 0 and env.fk.where(own) == []
    assert any(e.get("id") == 900 for e in env.fk.events)


def test_move_it_in_my_plan_too_replaces_the_old_event_once_the_new_one_is_out(env):
    ss = _with_own_saturday(env)
    _run(env)
    old, new = _own_id(env, D["sat"]), _own_id(env, D["sun"])
    env.fk.drag(old, D["sun"])
    # F2's "Move it in my plan too": the own row moves, moved_from the old day.
    ss[_at(ss, D["sat"])] = _rest(D["sat"])
    ss[_at(ss, D["sun"])] = _own_row(D["sun"], moved_from=D["sat"])
    _write_plan(ss)
    mark = len(env.fk.calls)
    r = _run(env)
    posted, deleted = _touched(env, mark)
    assert new in posted and old in deleted
    assert env.fk.where(new) == [D["sun"]] and env.fk.where(old) == []
    assert r["held"] == 0 and r["own_moved"] == []
    assert _counts(_run(env)) == (0, 0, 0)


def test_a_held_ride_moved_in_domestique_to_a_third_day_leaves_one_event(env):
    """The rider moved the ride's event Sat → Sun in intervals.icu, then
    moved the ride in Domestique from Sat to Thu: one event, on Thursday."""
    ss = _with_own_saturday(env)
    _run(env)
    old, new = _own_id(env, D["sat"]), _own_id(env, D["thu"])
    env.fk.drag(old, D["sun"])
    assert _run(env)["held"] == 1
    ss[_at(ss, D["sat"])] = _rest(D["sat"])
    ss[_at(ss, D["thu"])] = _own_row(D["thu"], moved_from=D["sat"])
    _write_plan(ss)
    r = _run(env)
    own_events = sorted((e["external_id"], e["start_date_local"][:10]) for e in env.fk.events
                        if str(e.get("external_id") or "").endswith(":own"))
    assert own_events == [(new, D["thu"])]
    assert r["held"] == 0 and r["own_moved"] == []
    assert _counts(_run(env)) == (0, 0, 0)


def test_two_rides_from_one_day_replace_the_old_event_once_both_are_out(env, monkeypatch):
    """Either ride may be the event's: nothing is guessed. While one of them
    cannot be sent the old event stays; once both are out it goes (restated
    in Wave 4: it used to stay held for good)."""
    ss = _with_own_saturday(env)
    _run(env)
    old = _own_id(env, D["sat"])
    env.fk.drag(old, D["fri"])
    ss[_at(ss, D["sat"])] = _rest(D["sat"])
    ss[_at(ss, D["thu"])] = _own_row(D["thu"], name="Ride B", moved_from=D["sat"])
    ss[_at(ss, D["sun"])] = _own_row(D["sun"], name="Ride A", moved_from=D["sat"])
    _write_plan(ss)
    rejecting = RejectsFileless(events=env.fk.events)
    rejecting._next_id = env.fk._next_id
    monkeypatch.setattr(icp, "_http", rejecting)
    icp.reconcile()
    assert rejecting.where(old) == [D["fri"]]            # neither ride is out yet
    env.fk = WindowedFakeICU(events=rejecting.events)
    env.fk._next_id = rejecting._next_id
    monkeypatch.setattr(icp, "_http", env.fk)
    mark = len(env.fk.calls)
    _run(env)
    _posted, deleted = _touched(env, mark)
    assert old in deleted
    own_events = sorted((e["external_id"], e["start_date_local"][:10]) for e in env.fk.events
                        if str(e.get("external_id") or "").endswith(":own"))
    assert own_events == sorted([(_own_id(env, D["thu"]), D["thu"]),
                                 (_own_id(env, D["sun"]), D["sun"])])


def test_a_second_ride_on_the_old_day_is_not_mistaken_for_an_agreed_move(env):
    """Ride A moved Sat → Sun earlier (moved_from Sat stays on it); ride B
    was added on Saturday since. The rider drags B's event onto Sunday:
    that is B's move, held, not A's old one."""
    ss = _week(mon="sweet.zwo")
    ss[_at(ss, D["sun"])] = _own_row(D["sun"], name="Ride A", moved_from=D["sat"])
    ss[_at(ss, D["sat"])] = _own_row(D["sat"], name="Ride B")
    _seed(env, ss)
    b = _own_id(env, D["sat"])
    env.fk.drag(b, D["sun"])
    mark = len(env.fk.calls)
    r = _run(env)
    posted, deleted = _touched(env, mark)
    assert b not in posted | deleted and env.fk.where(b) == [D["sun"]]
    assert r["own_moved"] == [{"day": D["sat"], "icu_day": D["sun"]}]


def test_the_old_event_stays_while_the_new_one_cannot_be_pushed(env, monkeypatch):
    ss = _with_own_saturday(env)
    _run(env)
    old = _own_id(env, D["sat"])
    env.fk.drag(old, D["sun"])
    rejecting = RejectsFileless(events=env.fk.events)
    rejecting._next_id = env.fk._next_id
    env.fk = rejecting
    monkeypatch.setattr(icp, "_http", rejecting)
    ss[_at(ss, D["sat"])] = _rest(D["sat"])
    ss[_at(ss, D["sun"])] = _own_row(D["sun"], moved_from=D["sat"])
    _write_plan(ss)
    mark = len(env.fk.calls)
    r = icp.reconcile()
    _posted, deleted = _touched(env, mark)
    assert {"day": D["sun"], "reason": "own_rejected"} in r["skipped"]
    assert old not in deleted and env.fk.where(old) == [D["sun"]]


def test_put_it_back_re_dates_the_event_while_it_is_still_on_that_day(env):
    ss = _with_own_saturday(env)
    _run(env)
    own = _own_id(env, D["sat"])
    env.fk.drag(own, D["sun"])
    assert _run(env)["held"] == 1
    # F2's "Put it back on Saturday".
    ss[_at(ss, D["sat"])] = _own_row(D["sat"], own_extra={"put_back_from": D["sun"]})
    _write_plan(ss)
    r = _run(env)
    assert env.fk.where(own) == [D["sat"]]
    assert r["held"] == 0 and r["own_moved"] == []


def test_put_it_back_never_follows_a_later_move(env):
    ss = _with_own_saturday(env)
    _run(env)
    own = _own_id(env, D["sat"])
    env.fk.drag(own, D["sun"])
    ss[_at(ss, D["sat"])] = _own_row(D["sat"], own_extra={"put_back_from": D["sun"]})
    _write_plan(ss)
    env.fk.drag(own, D["thu"])                          # moved again before the run
    r = _run(env)
    assert env.fk.where(own) == [D["thu"]]
    assert r["own_moved"] == [{"day": D["sat"], "icu_day": D["thu"]}]


def test_a_copy_of_our_own_ride_is_held_for_good(env):
    ss = _with_own_saturday(env)
    _run(env)
    own = _own_id(env, D["sat"])
    env.fk.copy(own, D["sun"])
    ss[_at(ss, D["sat"])] = _own_row(D["sat"], own_extra={"put_back_from": D["sun"]})
    _write_plan(ss)
    mark = len(env.fk.calls)
    r = _run(env)
    posted, deleted = _touched(env, mark)
    assert own not in posted | deleted
    assert env.fk.where(own) == [D["sat"], D["sun"]] and r["held"] == 1


def test_an_own_ride_moved_into_the_past_is_never_re_dated(env, freeze_clock):
    ss = _week(mon="sweet.zwo")
    ss[_at(ss, D["thu"])] = _own_row(D["thu"])
    _seed(env, ss)
    own = _own_id(env, D["thu"])
    env.fk.drag(own, D["mon"])                          # ridden early, today
    freeze_clock(date.fromisoformat(D["tue"]))
    ss[_at(ss, D["thu"])] = _own_row(D["thu"], own_extra={"put_back_from": D["mon"]})
    _write_plan(ss)
    mark = len(env.fk.calls)
    r = _run(env)
    posted, deleted = _touched(env, mark)
    assert own not in posted | deleted and env.fk.where(own) == [D["mon"]]
    assert r["own_moved"] == [] and r["held"] == 0     # past: no longer news


# ── #33 never learns an own row; the hold of a planner id is unchanged ──────

def test_learning_never_carries_an_own_ride_off_its_day(env):
    ss = _week(mon="sweet.zwo", sat="vo2.zwo")
    _seed(env, ss)
    env.fk.drag(_eid(env, D["sat"]), D["sun"])          # the VO2 to the Sunday rest day
    ss[_at(ss, D["sat"])] = _own_row(D["sat"])          # and the club ride on Saturday
    _write_plan(ss)
    r = _run(env)
    assert r["learned"] == []
    sat = next(s for w in _plan()["weeks"] for s in w["sessions"] if s["day"] == D["sat"])
    assert sat["own"]["name"] == "Saturday club ride"
    assert env.fk.where(_eid(env, D["sat"])) == [D["sun"]]
    assert env.fk.where(_own_id(env, D["sat"])) == [D["sat"]]


def test_a_moved_workout_on_the_day_does_not_block_the_own_ride(env):
    """T-P3: the rider moved Saturday's VO2 to next week in intervals.icu
    (held), then added their club ride on Saturday."""
    ss = _week(mon="sweet.zwo", sat="vo2.zwo")
    _seed(env, ss)
    next_wed = (MON + timedelta(days=9)).isoformat()
    env.fk.drag(_eid(env, D["sat"]), next_wed)
    ss[_at(ss, D["sat"])] = _own_row(D["sat"])
    _write_plan(ss)
    mark = len(env.fk.calls)
    r = _run(env)
    posted, deleted = _touched(env, mark)
    assert _own_id(env, D["sat"]) in posted
    assert _eid(env, D["sat"]) not in posted | deleted
    assert env.fk.where(_eid(env, D["sat"])) == [next_wed] and r["held"] == 1


def test_a_moved_workout_never_agrees_with_an_own_ride_the_plan_moved(env, monkeypatch):
    """The plan moved an own ride Sat → Sun; Saturday has its VO2 back. The
    rider's Sunday VO2 (moved there in intervals.icu) is not that move."""
    monkeypatch.setattr(icp, "_learnable_moves", lambda *a, **k: [])
    ss = _week(mon="sweet.zwo", sat="vo2.zwo")
    _seed(env, ss)
    sat0 = _eid(env, D["sat"])
    env.fk.drag(sat0, D["sun"])
    ss[_at(ss, D["sun"])] = _own_row(D["sun"], moved_from=D["sat"])
    _write_plan(ss)
    mark = len(env.fk.calls)
    r = _run(env)
    posted, deleted = _touched(env, mark)
    assert sat0 not in posted | deleted and env.fk.where(sat0) == [D["sun"]]
    assert r["held"] == 1


# ── C4: own rides follow the sync window ────────────────────────────────────

def test_own_rides_follow_the_sync_window(env):
    far = (MON + timedelta(days=30)).isoformat()
    ss = [_sess(D["mon"], zwo="sweet.zwo")] + [
        _own_row(far) if d == far else _rest(d)
        for d in ((MON + timedelta(days=i)).isoformat() for i in range(1, 49))]
    _write_plan(ss)
    own = _own_id(env, far)
    mark = len(env.fk.calls)
    _run(env)
    assert own not in _touched(env, mark)[0] and env.fk.where(own) == []   # 2 weeks
    env.pm.save_prefs({"icu_calendar_horizon_days": 42})
    _run(env)
    assert env.fk.where(own) == [far]
    env.pm.save_prefs({"icu_calendar_horizon_days": icp.HORIZON_DAYS})
    mark = len(env.fk.calls)
    _run(env)
    assert own in _touched(env, mark)[1] and env.fk.where(own) == []
    env.pm.save_prefs({"icu_calendar_horizon_days": 42})
    _run(env)
    assert env.fk.where(own) == [far]
    assert not icp.sweep_all().get("error") and env.fk.where(own) == []


# ── C6: the Send button refuses an own-ride day ─────────────────────────────

def test_send_refuses_an_own_ride_day(env):
    _with_own_saturday(env)
    _run(env)
    mark = len(env.fk.calls)
    r = env.client.post("/api/calendar/push-workout", json={
        "source": "planner", "zwo_file": "vo2.zwo", "date": D["sat"]}).json()
    assert r == {"error": "own_ride_day"}
    assert not [c for c in env.fk.calls[mark:] if c[0] == "POST"]


# ═══ Wave 4 (QA): stale moved_from, O8, failed uploads, the moved report ════

def test_an_earlier_ride_from_the_same_day_never_doubles_the_followed_ride(env):
    """QA S1 / M3: ride A left Saturday long ago (it is out on Sunday under
    its own id and keeps moved_from Saturday; the rider has since moved that
    event to Thursday, so A is not in this run's upsert). Ride B was added
    on Saturday, pushed, dragged to Friday and followed there in Domestique:
    one event for B, on Friday."""
    ss = _week(mon="sweet.zwo")
    ss[_at(ss, D["sun"])] = _own_row(D["sun"], name="Ride A", moved_from=D["sat"])
    ss[_at(ss, D["sat"])] = _own_row(D["sat"], name="Ride B", minutes=60, tss=50.0)
    _seed(env, ss)
    a, b_old, b_new = _own_id(env, D["sun"]), _own_id(env, D["sat"]), _own_id(env, D["fri"])
    env.fk.drag(a, D["thu"])
    env.fk.drag(b_old, D["fri"])
    ss[_at(ss, D["sat"])] = _rest(D["sat"])
    ss[_at(ss, D["fri"])] = _own_row(D["fri"], name="Ride B", minutes=60, tss=50.0,
                                     moved_from=D["sat"])
    _write_plan(ss)
    _run(env)
    on_fri = sorted(e["external_id"] for e in env.fk.events
                    if e["start_date_local"][:10] == D["fri"])
    assert on_fri == [b_new]
    assert env.fk.where(a) == [D["thu"]]                 # A's move stays held


def test_a_removed_ride_never_touches_a_copy_or_the_riders_own_events(env):
    """O8 removes only Domestique's own event: a copy the rider made of it
    (two events, one id) and the rider's own events stay."""
    ss = _with_own_saturday(env)
    _run(env)
    own = _own_id(env, D["sat"])
    env.fk.copy(own, D["sun"])                           # the rider repeats it
    env.fk.events.append({"id": 901, "category": "WORKOUT", "type": "Ride",
                          "name": "Sunday spin", "start_date_local": f"{D['sun']}T00:00:00"})
    ss[_at(ss, D["sat"])] = _rest(D["sat"])
    _write_plan(ss)
    mark = len(env.fk.calls)
    _run(env)
    _posted, deleted = _touched(env, mark)
    assert own not in deleted and env.fk.where(own) == [D["sat"], D["sun"]]
    assert any(e.get("id") == 901 for e in env.fk.events)


def test_a_removed_ride_whose_event_was_moved_into_the_past_stays(env, freeze_clock):
    ss = _week(mon="sweet.zwo")
    ss[_at(ss, D["thu"])] = _own_row(D["thu"])
    _seed(env, ss)
    own = _own_id(env, D["thu"])
    env.fk.drag(own, D["mon"])                           # ridden early, today
    freeze_clock(date.fromisoformat(D["tue"]))
    ss[_at(ss, D["thu"])] = _rest(D["thu"])
    _write_plan(ss)
    mark = len(env.fk.calls)
    _run(env)
    _posted, deleted = _touched(env, mark)
    assert own not in deleted and env.fk.where(own) == [D["mon"]]


@pytest.mark.parametrize("status,error", [(0, "network"), (503, "http_503")])
def test_a_ride_that_could_not_be_sent_is_an_error_and_nothing_is_swept(env, monkeypatch,
                                                                         status, error):
    """L4: no answer, or a server error, on the rides' POST is not
    intervals.icu refusing the ride: the run says so, as for the workouts,
    and the workout the ride replaced stays until the next run."""
    _with_own_saturday(env)
    fk = env.fk

    def http(method, path, payload=None, timeout=30.0):
        if method == "POST" and any("file_contents_base64" not in ev for ev in payload):
            fk.calls.append((method, path, payload))
            return status, b"gateway timeout"
        return fk(method, path, payload, timeout)

    monkeypatch.setattr(icp, "_http", http)
    mark = len(fk.calls)
    r = icp.reconcile()
    assert r["error"] == error
    assert not [s for s in r["skipped"] if s["reason"] == "own_rejected"]
    assert not [c for c in fk.calls[mark:] if c[0] == "PUT"]
    assert fk.where(_eid(env, D["sat"])) == [D["sat"]]


def test_a_put_back_whose_upload_was_refused_is_still_reported_and_kept(env, monkeypatch):
    """QA P1: the run released the hold, but intervals.icu refused the ride:
    it is still where the rider moved it, so the run still reports it, the
    note after the run keeps the put-back, and the next run puts it back."""
    import app as app_module
    monkeypatch.setattr(app_module, "_icu_push_last_result", None)
    ss = _with_own_saturday(env)
    _run(env)
    own = _own_id(env, D["sat"])
    env.fk.drag(own, D["sun"])
    _run(env)
    ss[_at(ss, D["sat"])] = _own_row(D["sat"], own_extra={
        "put_back_from": D["sun"], "put_back_at": "2026-10-18T08:00:00"})
    _write_plan(ss)
    ok_fake = env.fk
    rejecting = RejectsFileless(events=ok_fake.events)
    rejecting._next_id = ok_fake._next_id
    monkeypatch.setattr(icp, "_http", rejecting)
    r = icp.reconcile()
    assert {"day": D["sat"], "reason": "own_rejected"} in r["skipped"]
    assert r["own_moved"] == [{"day": D["sat"], "icu_day": D["sun"]}]
    app_module._icu_push_note_result(r)
    sat = next(s for w in _plan()["weeks"] for s in w["sessions"] if s["day"] == D["sat"])
    assert sat["own"]["put_back_from"] == D["sun"]
    env.fk = WindowedFakeICU(events=rejecting.events)
    env.fk._next_id = rejecting._next_id
    monkeypatch.setattr(icp, "_http", env.fk)
    r = _run(env)
    assert env.fk.where(own) == [D["sat"]] and r["own_moved"] == []
    app_module._icu_push_note_result(r)
    sat = next(s for w in _plan()["weeks"] for s in w["sessions"] if s["day"] == D["sat"])
    assert "put_back_from" not in sat["own"]


def test_a_run_that_could_not_read_the_calendar_reports_no_moves(env):
    """QA P1: a failed read says nothing about moved rides, so nothing can
    take it for "every ride is back on its day"."""
    _write_plan(_week(mon="sweet.zwo"))
    env.fk.fail["GET"] = (0, b"timed out")
    r = icp.reconcile()
    assert r["error"] == "network" and "own_moved" not in r


@pytest.mark.parametrize("how", ["sync_off", "disconnect"])
def test_after_sync_off_or_disconnect_no_move_is_left_to_ask_about(env, how):
    """QA-UX QX-M2: the sweep took every event of ours, the moved ride's
    included: the ride's dialog must stop asking about it."""
    _with_own_saturday(env)
    _run(env)
    env.fk.drag(_own_id(env, D["sat"]), D["sun"])
    assert env.client.post("/api/icu/push", json={}).json()["ok"]
    assert env.client.get("/api/icu/push").json()["own_moved"] == [
        {"day": D["sat"], "icu_day": D["sun"]}]
    if how == "sync_off":
        assert env.client.post("/api/icu/push", json={"sync": False}).json()["ok"]
    else:
        assert env.client.post("/api/icu/disconnect").json()["ok"]
    assert env.client.get("/api/icu/push").json()["own_moved"] == []


def test_a_restart_still_shows_the_moved_ride(env, monkeypatch):
    """QA-UX QX-M2, the other way round: the move is kept per rider, not
    only in the memory of the run that saw it."""
    import app as app_module
    _with_own_saturday(env)
    _run(env)
    env.fk.drag(_own_id(env, D["sat"]), D["sun"])
    assert env.client.post("/api/icu/push", json={}).json()["ok"]
    monkeypatch.setattr(app_module, "_icu_push_last_result", None)     # a restart
    assert env.client.get("/api/icu/push").json()["own_moved"] == [
        {"day": D["sat"], "icu_day": D["sun"]}]


@pytest.mark.parametrize("then", ["a_failed_run", "the_other_riders_run"])
def test_held_days_survive_a_failed_run_and_the_other_riders_run(env, monkeypatch, then):
    """QA C9b: the plan days whose workout the rider moved in intervals.icu
    stay known after a run that could not read the calendar, and after the
    other rider's run, so the own-ride writer still leaves them alone."""
    import app as app_module
    monkeypatch.setattr(app_module, "_icu_push_last_result", None)
    _seed(env, _week(mon="sweet.zwo", wed="steady.zwo", thu="vo2.zwo"))
    env.fk.drag(_eid(env, D["wed"]), D["thu"])          # onto a training day: held
    assert env.client.post("/api/icu/push", json={}).json()["held_days"] == [D["wed"]]
    if then == "a_failed_run":
        env.fk.fail["GET"] = (0, b"timed out")
        assert env.client.post("/api/icu/push", json={}).json()["error"] == "network"
        del env.fk.fail["GET"]
    else:
        pid_b = env.pm.create_profile("Other rider")
        env.pm.switch(pid_b)
        env.pm.save_env("i456", "KEY456")
        _write_plan(_week(mon="sweet.zwo"))
        assert env.client.post("/api/icu/push", json={}).json()["ok"]
        env.pm.switch(env.pid)
    assert app_module._icu_held_plan_days() == [D["wed"]]
