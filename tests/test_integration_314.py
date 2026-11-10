"""3.14.0 QA — integration across the wellness import (F1), the rider's own
rides (F2) and the longer calendar sync window (F3).

Each test drives the features where they meet, through what a rider uses:
the own-ride dialog and the import list (POST /api/plan/own-sessions,
GET /api/icu/planned-rides), the Push button and the window setting
(POST /api/icu/push), the Send button (POST /api/calendar/push-workout),
Today, Ease today, auto-adjust, a rebuild and a new plan. It asserts what the
rider would see: events in the intervals.icu calendar, plan rows on disk,
endpoint answers. Ids follow grill_integration §9 (I1-I10), the owner's
O-decisions and the coordinator's C-decisions of 3.14.0; "qa_*" tests are
seams this QA wave found.

Hermetic: the stubbed HOME of tests/test_icu_push.py; intervals.icu only
through fakes behind icu_calendar_push._http (a windowed calendar that
deletes as the public API documents and holds the rider's own events); the
wellness fetch patched; the clock frozen. No network, no live data. No
Hooper threshold and no window length is written out here: they come from
readiness_composite and icu_calendar_push.
"""
from __future__ import annotations

import json
import math
import shutil
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import app as app_module
import clock
import db
import icu_calendar_push as icp
import readiness_composite as rc
import training_planner as tp
from test_icu_push import stub, _sess, _apikey, STEADY_ZWO  # noqa: F401  (stub is a fixture)
from test_icu_hold_28 import WindowedFakeICU, MON
from test_331_surfaces import _extract_js_function, _run_node

DASHBOARD = Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html"
DEFAULT = icp.HORIZON_DAYS
WIDE = max(icp.HORIZON_CHOICES)
LOOK = icp._LOOKAHEAD_DAYS
NOON = datetime(MON.year, MON.month, MON.day, 12, 0, 0)
GOAL = {"goal_type": "continuous", "max_weekday_hours": 5.0, "max_weekend_hours": 6.0}
NEG, NORMAL = rc.HOOPER_NEGATIVE_ANSWER, rc.HOOPER_NORMAL_ANSWER


def D(i: int) -> str:
    """The day ``i`` days after MON (a Monday), as the plan writes it."""
    return (MON + timedelta(days=i)).isoformat()


# ── fakes ────────────────────────────────────────────────────────────────────

class Calendar(WindowedFakeICU):
    """The rider's intervals.icu calendar. WindowedFakeICU's GET honours
    oldest/newest, ``drag``/``copy`` keep our external_id, an upsert keeps
    the keys it leaves out. The bulk delete works as the public API
    documents (a row naming an external_id deletes every event under it; a
    row with only an id deletes that one event), and the calendar holds the
    rider's own events, which carry no external id. ``reject_fileless``:
    intervals.icu refusing a batch that holds an event without a workout
    file (the owner's live check decides whether it does)."""

    reject_fileless = False

    def __call__(self, method, path, payload=None, timeout=30.0):
        if (method == "POST" and self.reject_fileless
                and any("file_contents_base64" not in ev for ev in payload)):
            self.calls.append((method, path, payload))
            return 422, b'{"error":"moving_time is not allowed here"}'
        if method == "PUT" and "PUT" not in self.fail:
            self.calls.append((method, path, payload))
            ext = {r["external_id"] for r in payload if r.get("external_id")}
            ids = {r["id"] for r in payload if not r.get("external_id")}
            self.events = [e for e in self.events
                           if not (e.get("external_id") and e["external_id"] in ext)
                           and e.get("id") not in ids]
            return 200, b"{}"
        return super().__call__(method, path, payload, timeout)

    def rider_event(self, day, eid, name="Saturday club ride", minutes=180, load=180):
        """A ride the rider planned in intervals.icu themselves."""
        self.events.append({"id": eid, "start_date_local": f"{day}T00:00:00",
                            "category": "WORKOUT", "type": "Ride", "name": name,
                            "moving_time": minutes * 60, "icu_training_load": load})
        return eid


class Calendars:
    """One Calendar per intervals.icu athlete, picked from the path, so two
    riders' calendars can be told apart. ``on_get`` runs once, inside the
    next GET (a profile switch landing while a run reads the calendar)."""

    def __init__(self):
        self.of: dict = {}
        self.calls: list = []
        self.on_get = None

    def cal(self, athlete_id):
        return self.of.setdefault(athlete_id, Calendar())

    def __call__(self, method, path, payload=None, timeout=30.0):
        athlete_id = path.split("/")[1]
        self.calls.append((athlete_id, method, path))
        if method == "GET" and self.on_get is not None:
            hook, self.on_get = self.on_get, None
            hook()
        return self.cal(athlete_id)(method, path, payload, timeout)


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def env(stub, freeze_clock, monkeypatch):
    freeze_clock(NOON)
    _apikey(stub)
    for i in range(7 * 7):
        (stub.workouts / f"w{i:02d}.zwo").write_text(
            STEADY_ZWO.replace("Steady Test", f"W{i:02d}"), encoding="utf-8")
    stub.fk = Calendar()
    monkeypatch.setattr(icp, "_http", stub.fk)
    monkeypatch.setattr(app_module, "_icu_push_last_result", None)
    monkeypatch.setattr(app_module, "_icu_push_last_daily", None)
    monkeypatch.setattr(app_module, "_kick_lazy_icu_sync", lambda *a, **k: None)
    icp._RECENT_PUSH.clear()
    yield stub
    icp._RECENT_PUSH.clear()


def _row(i, st="z2", **kw):
    if st == "rest":
        return _sess(D(i), zwo="", stype="rest", duration_min=0, tss_estimate=0,
                     description="Rest", **kw)
    return _sess(D(i), zwo=f"w{i:02d}.zwo", stype=st,
                 tss_estimate=75.0 if st == "vo2max" else 45.0, **kw)


def _plan(hard=(), weeks=7, budget=900, rows=None):
    """Mon-Sun weeks from MON: rest on Mondays and Fridays, Z2 elsewhere, a
    VO2max session on each day in ``hard``; one workout file per day, so a
    workout can be told apart wherever it sits. ``rows`` replaces days."""
    rows = rows or {}
    out = []
    for k in range(weeks):
        ss = []
        for i in range(7 * k, 7 * k + 7):
            if i in rows:
                ss.append(rows[i])
            elif (MON + timedelta(days=i)).weekday() in (0, 4):
                ss.append(_row(i, "rest"))
            else:
                ss.append(_row(i, "vo2max" if i in hard else "z2"))
        out.append({"week_num": k + 1, "start": D(7 * k), "end": D(7 * k + 6),
                    "phase": "base", "tss_target": budget, "is_stepback": False,
                    "sessions": ss})
    return {"goal": dict(GOAL), "generated": "2026-10-01T00:00:00", "weeks": out}


def _plan_path() -> Path:
    return Path(tp.PLAN_DIR) / "current_plan.json"


def _write(plan):
    p = _plan_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan), encoding="utf-8")


def _read() -> dict:
    return json.loads(_plan_path().read_text(encoding="utf-8"))


def _day(plan, day):
    day = D(day) if isinstance(day, int) else day
    rows = [s for w in plan["weeks"] for s in w["sessions"] if s["day"] == day]
    assert len(rows) == 1, (day, rows)
    return rows[0]


def _push(env, ok=True):
    """The Push button: one calendar run, noted for the status endpoint."""
    j = env.client.post("/api/icu/push", json={}).json()
    if ok:
        assert j.get("ok") and not j.get("error") and not j.get("needs_reconnect"), j
    return j


def _status(env) -> dict:
    return env.client.get("/api/icu/push").json()


def _window(env, n) -> dict:
    """The window setting (with sync on, the calendar follows at once)."""
    j = env.client.post("/api/icu/push", json={"horizon_days": n}).json()
    assert j.get("horizon_days") == n, j
    return j


def _sync_on(env):
    env.pm.save_prefs({"icu_calendar_sync": True})


def _own(env, status=200, **body) -> dict:
    """The own-ride dialog / import list."""
    r = env.client.post("/api/plan/own-sessions", json=body)
    assert r.status_code == status, r.text[:500]
    return r.json()


def _ride(i, name="Club ride", minutes=180, intensity="easy", **kw):
    return dict({"date": D(i), "name": name, "duration_min": minutes,
                 "intensity": intensity}, **kw)


def _import_item(x) -> dict:
    """What the import dialog posts for a ticked row of the list."""
    item = {"date": x["date"], "name": x["name"], "icu_event_id": x["event_id"],
            "icu_day": x["date"], "duration_min": x["duration_min"] or 120,
            "intensity": x["intensity"]}
    if x["load"] is not None:
        item.update(load=x["load"], load_from=x["load_from"])
    if x["status"] == "day_has_your_ride":
        item["link"] = True
    return item


def _listed(env, event_id):
    lst = env.client.get("/api/icu/planned-rides").json()
    return lst, next(x for x in lst["rides"] if x["event_id"] == event_id)


def _send(env, i):
    return env.client.post("/api/calendar/push-workout", json={
        "source": "planner", "zwo_file": f"w{i:02d}.zwo", "date": D(i)}).json()


def _eid(env, i, n=0):
    return f"domestique:{env.pid}:{D(i)}:{n}"


def _own_id(env, i):
    return f"domestique:{env.pid}:{D(i)}:own"


def _ours(env, fk=None) -> dict:
    """{external_id: [days]} for every event of this rider's in the calendar."""
    prefix = f"domestique:{env.pid}:"
    out: dict = {}
    for e in (fk or env.fk).events:
        if str(e.get("external_id") or "").startswith(prefix):
            out.setdefault(e["external_id"], []).append(e["start_date_local"][:10])
    return {k: sorted(v) for k, v in out.items()}


def _event(env, eid) -> dict:
    hits = [e for e in env.fk.events if e.get("external_id") == eid]
    assert len(hits) == 1, (eid, len(hits))
    return hits[0]


def _touched(env, since=0):
    """external_ids named in POST / PUT payloads after call index ``since``."""
    posted, deleted = set(), set()
    for method, _path, payload in env.fk.calls[since:]:
        if method == "POST":
            posted |= {ev["external_id"] for ev in payload}
        elif method == "PUT":
            deleted |= {r.get("external_id") for r in payload}
    return posted, deleted


def _pushable(env, horizon) -> dict:
    """What the calendar should hold for the plan on disk: each pushable
    session from today to today+horizon once, under its id (a planned
    workout ``<day>:<n>``, a ride added in Domestique ``<day>:own``, never a
    ride imported from intervals.icu): {external_id: day}."""
    today = clock.today()
    by_day: dict = {}
    for w in _read()["weeks"]:
        for s in w["sessions"]:
            by_day.setdefault(s["day"], []).append(s)
    want = {}
    for day, rows in by_day.items():
        if not today <= date.fromisoformat(day) <= today + timedelta(days=horizon):
            continue
        for n, s in enumerate(rows):
            st = str(s.get("status") or "")
            if (s.get("session_type") == "rest" or s.get("is_race")
                    or st == "dismissed" or st.startswith("moved_from")):
                continue
            own = s.get("own")
            if own:
                if own.get("source") != "icu" and not own.get("icu_event_id"):
                    want[f"domestique:{env.pid}:{day}:own"] = day
            elif s.get("zwo_file"):
                want[f"domestique:{env.pid}:{day}:{n}"] = day
    return want


def _assert_mirror(env, horizon, moved=None):
    """From today on, the calendar holds each pushable session exactly once,
    on its day (or where the rider moved it: ``moved``), and nothing else of
    ours (past days are never touched, so they are not judged)."""
    moved = moved or {}
    today = clock.today().isoformat()
    want = _pushable(env, horizon)
    ours = {e: ds for e, ds in _ours(env).items() if max(ds) >= today}
    for eid, day in want.items():
        assert ours.get(eid) == [moved.get(eid, day)], (eid, ours.get(eid))
    for eid, day in moved.items():
        assert ours.get(eid) == [day], (eid, ours.get(eid))
    extra = {e: d for e, d in ours.items() if e not in want and e not in moved}
    assert not extra, f"events of ours the plan does not hold: {extra}"
    own_days = [d for e, ds in ours.items() if e.endswith(":own") for d in ds]
    assert len(own_days) == len(set(own_days)), f"two own-ride events on one day: {own_days}"


def _state_path(env, pid):
    return env.pm._profiles_dir / pid / "icu_calendar_push.json"


def _rows(db_file, table) -> int:
    if not Path(db_file).exists():
        return 0
    con = sqlite3.connect(str(db_file))
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


def _other_rider(env, sync=False) -> str:
    """Rider B: the defaults (2 weeks, sync off unless asked), connected with
    an API key for another athlete, with a plan; A is active again after."""
    pid_b = env.pm.create_profile("Other rider")
    env.pm.switch(pid_b)
    env.pm.save_env("i456", "KEY456")
    _write(_plan())
    if sync:
        _sync_on(env)
    env.pm.switch(env.pid)
    db.shutdown_sync()                      # the switches leave the stop flag set
    return pid_b


def _spy_lock_wait(monkeypatch):
    """Event set when a calendar run reaches the reconcile lock."""
    arrived, real = threading.Event(), icp._one_at_a_time

    def spy(timeout_s, fn, *args):
        if fn.__name__ == "_reconcile_unless_swept":
            arrived.set()
        return real(timeout_s, fn, *args)

    monkeypatch.setattr(icp, "_one_at_a_time", spy)
    return arrived


# ── the morning check-in from intervals.icu (F1), by the constants ──────────

def _code(field, stored) -> int:
    """The intervals.icu answer to ``field`` that the import stores as
    ``stored`` (the form's button value)."""
    for c in range(5):
        got = db.icu_check_in({field: c}) or {}
        if got.get(field) == stored:
            return c
    raise AssertionError(f"no intervals.icu answer to {field} stores {stored}")


def _soreness_gate():
    """Sore legs (the negative answer): the soreness gate wants a recovery ride."""
    return {"soreness": _code("soreness", NEG)}


def _fixed_rest_limit():
    """The fewest negative answers (fatigue, stress) that lift an otherwise
    Normal check-in to the fixed rest limit, soreness Normal, so only the
    check-in band (G6) acts."""
    k = math.ceil((rc.HOOPER_FIXED_REST - 4 * NORMAL) / (NEG - NORMAL))
    assert 0 < k <= 2, k
    rec = {"soreness": _code("soreness", NORMAL)}
    for f in ("fatigue", "stress")[:k]:
        rec[f] = _code(f, NEG)
    return rec


def _injury():
    """An injury logged as Poor in intervals.icu (code 3, the intervals.icu
    scale): it counts as sore legs (grill_wellness M3)."""
    return {"injury": 3}


CHECK_INS = {"soreness_gate": _soreness_gate, "fixed_rest_limit": _fixed_rest_limit,
             "injury": _injury}


def _import_check_in(record, day=0):
    rec = {"id": D(day), "ctl": 40.0, "atl": 38.0, **record}
    with patch.object(db, "fetch_wellness", return_value=[rec]):
        assert db.sync_wellness(days=7) == 1


# ═══ A. own rides in the intervals.icu calendar (F2 writer × F3 engine) ═════

def test_i1_c1_a_ride_added_in_domestique_goes_out_plain_and_the_workout_it_replaced_goes(env):
    """I1 / C1 / C2 / X3 / X3b: at 6 weeks, a hard club ride added on day 30
    over its VO2max session. One run later the calendar holds a plain
    planned ride under ``<day>:own`` (no file, no steps), sent in a POST of
    its own after the workouts, and the VO2max event of that day is gone.
    The next run changes nothing and raises nothing."""
    _write(_plan(hard=(30,)))
    _window(env, WIDE)                                   # sync off: saved only
    _push(env)
    assert _event(env, _eid(env, 30))["filename"] == "w30.zwo"
    j = _own(env, set=[_ride(30, intensity="hard")])
    assert j["applied"] == [{"action": "add", "date": D(30)}]
    mark = len(env.fk.calls)
    r = _push(env)
    posts = [c[2] for c in env.fk.calls[mark:] if c[0] == "POST"]
    assert len(posts) == 2 and [ev["external_id"] for ev in posts[1]] == [_own_id(env, 30)]
    assert _own_id(env, 30) not in {ev["external_id"] for ev in posts[0]}
    ev = _event(env, _own_id(env, 30))
    assert "filename" not in ev and "file_contents_base64" not in ev
    assert (ev["start_date_local"], ev["type"], ev["name"], ev["moving_time"]) == (
        f"{D(30)}T00:00:00", "Ride", "Club ride", 180 * 60)
    assert ev["icu_training_load"] == round(_day(_read(), 30)["tss_estimate"])
    assert _eid(env, 30) not in _ours(env)
    assert not [s for s in r["skipped"] if str(s.get("reason")).startswith("own")]
    _assert_mirror(env, WIDE)
    r2 = _push(env)
    assert (r2["pushed"], r2["updated"], r2["deleted"]) == (0, 0, 0)
    # F2's import list never offers the ride Domestique pushed (no duplicate).
    assert not [x for x in env.client.get("/api/icu/planned-rides").json()["rides"]
                if x["date"] == D(30)]


def test_i1_an_edit_reaches_the_same_event_and_a_removal_brings_the_workout_back(env):
    _write(_plan(hard=(30,)))
    _window(env, WIDE)
    _own(env, set=[_ride(30, intensity="hard")])
    _push(env)
    first = _event(env, _own_id(env, 30))["id"]
    _own(env, set=[_ride(30, minutes=150, intensity="hard")])
    r = _push(env)
    ev = _event(env, _own_id(env, 30))
    assert (ev["id"], ev["moving_time"]) == (first, 150 * 60) and r["updated"] >= 1
    _own(env, remove=[D(30)])
    _push(env)
    assert _own_id(env, 30) not in _ours(env)
    # The VO2max comes back (its file re-matched from the planner's library,
    # which in this stub does not hold the push folder's w30.zwo).
    back = _day(_read(), 30)
    assert back["session_type"] == "vo2max" and not back.get("own")
    assert _event(env, _eid(env, 30))["filename"] == back["zwo_file"]
    _assert_mirror(env, WIDE)


def test_i2_c2_a_rejected_ride_never_keeps_the_workouts_off_the_calendar(env):
    """I2: intervals.icu refuses the file-less ride; the workouts still land,
    the ride's day is a plain skip, and the Push button reports success."""
    env.fk.reject_fileless = True
    _write(_plan())
    _own(env, set=[_ride(10)])
    for _ in range(2):
        j = _push(env)
        assert {"day": D(10), "reason": "own_rejected"} in j["skipped"]
        workouts = {e for e in _pushable(env, DEFAULT) if not e.endswith(":own")}
        assert workouts and workouts <= set(_ours(env))
        assert not [e for e in _ours(env) if e.endswith(":own")]
        assert not (_status(env)["last_result"] or {}).get("error")


@pytest.mark.parametrize("by_id", [False, True], ids=["delete-by-external-id", "delete-by-event-id"])
def test_i3_c5_a_ride_imported_from_intervals_icu_is_never_sent_back_nor_deleted(env, monkeypatch, by_id):
    """I3 / C5 / C7: the rider's own intervals.icu ride, brought in through
    the import list, is never in a POST; the planned workout it replaced is
    deleted; the shrink 6 → 2 weeks and sync off (with either delete payload)
    never name it; it is still there, unchanged, at the end."""
    monkeypatch.setattr(icp, "_DELETE_BY_ID_ONLY", by_id)
    _write(_plan())
    _sync_on(env)
    _window(env, WIDE)
    rider = env.fk.rider_event(D(12), eid=9001)
    assert _eid(env, 12) in _ours(env)
    lst, x = _listed(env, rider)
    assert (x["status"], x["date"], x["duration_min"], x["load"]) == ("new", D(12), 180, 180)
    j = _own(env, profile_id=lst["profile_id"], set=[_import_item(x)])
    assert j["applied"] == [{"action": "add", "date": D(12)}]
    before = dict(next(e for e in env.fk.events if e.get("id") == rider))
    mark = len(env.fk.calls)
    _push(env)
    assert _eid(env, 12) not in _ours(env)               # the workout it replaced goes
    _assert_mirror(env, WIDE)
    _window(env, DEFAULT)                                # the shrink's clean-up
    assert env.client.post("/api/icu/push", json={"sync": False}).json()["sync_enabled"] is False
    for method, _p, payload in env.fk.calls[mark:]:
        if method == "POST":
            assert all(ev["start_date_local"][:10] != D(12) for ev in payload), payload
        elif method == "PUT":
            for row in payload:
                assert row.get("id") != rider, payload
                assert row.get("external_id") in (None, "") or str(
                    row["external_id"]).startswith(f"domestique:{env.pid}:"), payload
    assert next(e for e in env.fk.events if e.get("id") == rider) == before
    assert _ours(env) == {}
    assert _day(_read(), 12)["own"]["source"] == "icu"


def test_c5_a_gone_imported_ride_the_rider_keeps_stays_imported(env):
    _write(_plan())
    rider = env.fk.rider_event(D(12), eid=9001)
    lst, x = _listed(env, rider)
    _own(env, profile_id=lst["profile_id"], set=[_import_item(x)])
    _push(env)
    env.fk.events = [e for e in env.fk.events if e.get("id") != rider]   # deleted there
    gone = env.client.get("/api/icu/planned-rides").json()["gone"]
    assert [g["date"] for g in gone] == [D(12)]
    mark = len(env.fk.calls)
    _push(env)                                            # the rider keeps it
    posted, _ = _touched(env, mark)
    assert not [e for e in posted if D(12) in e]
    assert _day(_read(), 12)["own"]["source"] == "icu"
    _assert_mirror(env, DEFAULT)


def test_i4_c4_a_ride_added_in_domestique_follows_the_sync_window(env):
    """I4 / C4: a ride on day 30 is not in a 2-week calendar, appears at 6
    weeks, goes with the shrink back to 2, comes back at 6, and goes with
    everything else of ours when sync is turned off."""
    _write(_plan())
    _sync_on(env)
    _own(env, set=[_ride(30)])
    _push(env)
    assert _own_id(env, 30) not in _ours(env) and _status(env)["horizon_days"] == DEFAULT
    _window(env, WIDE)
    assert _ours(env).get(_own_id(env, 30)) == [D(30)] and _status(env)["horizon_days"] == WIDE
    _assert_mirror(env, WIDE)
    _window(env, DEFAULT)
    assert _own_id(env, 30) not in _ours(env)
    _assert_mirror(env, DEFAULT)
    _window(env, WIDE)
    assert _ours(env).get(_own_id(env, 30)) == [D(30)]
    env.client.post("/api/icu/push", json={"sync": False})
    assert _ours(env) == {}


def _held_ride(env, day=30, to=31, window=WIDE):
    """A ride added in Domestique on ``day`` and pushed, then dragged by the
    rider in intervals.icu to ``to``: one run later it is held there and the
    ride's dialog is told (own_moved, O4)."""
    _write(_plan())
    _sync_on(env)
    _window(env, window)
    _own(env, set=[_ride(day, intensity="moderate")])
    _push(env)
    icp._RECENT_PUSH.clear()                              # the rider acts later
    env.fk.drag(_own_id(env, day), D(to))
    mark = len(env.fk.calls)
    r = _push(env)
    posted, deleted = _touched(env, mark)
    assert _own_id(env, day) not in posted | deleted
    assert env.fk.where(_own_id(env, day)) == [D(to)] and r["held"] >= 1
    assert _status(env)["own_moved"] == [{"day": D(day), "icu_day": D(to)}]
    return r


def test_i7_o4_a_moved_ride_is_held_through_a_shrink_and_followed_on_request(env):
    """I7 / O4: the shrink never deletes the held ride and widening again
    never adds a second one; "Move it in my plan too" leaves one event on
    the new day and the planned workout back on the old one."""
    _held_ride(env)
    _window(env, DEFAULT)
    assert env.fk.where(_own_id(env, 30)) == [D(31)]
    _window(env, WIDE)
    assert env.fk.where(_own_id(env, 30)) == [D(31)] and _own_id(env, 31) not in _ours(env)
    j = _own(env, move=[{"from": D(30), "to": D(31)}])    # "Move it in my plan too"
    assert j["applied"] == [{"action": "move", "from": D(30), "to": D(31)}]
    _push(env)
    assert _ours(env).get(_own_id(env, 31)) == [D(31)] and _own_id(env, 30) not in _ours(env)
    assert _status(env)["own_moved"] == []
    _assert_mirror(env, WIDE)


def test_o4_a_held_ride_moved_in_domestique_to_a_third_day_leaves_one_event(env):
    _held_ride(env)
    _own(env, move=[{"from": D(30), "to": D(33)}])
    _push(env)
    assert _ours(env).get(_own_id(env, 33)) == [D(33)]
    assert not [e for e, ds in _ours(env).items() if e.endswith(":own") and D(31) in ds]
    assert _status(env)["own_moved"] == []
    _assert_mirror(env, WIDE)


def test_o4_put_it_back_redates_the_ride_and_a_later_drag_is_asked_about_again(env, freeze_clock):
    """O4 "Put it back": the next run re-dates the same event to the plan
    day; that run's note clears the put-back, so the same drag later is held
    and asked about again instead of being undone."""
    _held_ride(env, day=10, to=11, window=DEFAULT)
    first = _event(env, _own_id(env, 10))["id"]
    j = _own(env, put_back=[{"day": D(10), "icu_day": D(11)}])
    assert [a["action"] for a in j["applied"]] == ["put_back"]
    freeze_clock(NOON + timedelta(minutes=2))             # the next run comes later
    _push(env)
    ev = _event(env, _own_id(env, 10))
    assert (ev["id"], ev["start_date_local"][:10]) == (first, D(10))
    assert _status(env)["own_moved"] == []
    assert "put_back_from" not in _day(_read(), 10)["own"]
    icp._RECENT_PUSH.clear()
    env.fk.drag(_own_id(env, 10), D(11))                  # the same drag again
    freeze_clock(NOON + timedelta(minutes=5))
    mark = len(env.fk.calls)
    _push(env)
    posted, deleted = _touched(env, mark)
    assert _own_id(env, 10) not in posted | deleted
    assert env.fk.where(_own_id(env, 10)) == [D(11)]
    assert _status(env)["own_moved"] == [{"day": D(10), "icu_day": D(11)}]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "QA-INT P1: a put-back is cleared by a run that did not re-date the ride "
    "(own POST rejected; a failed run before the next own-ride save)"))
@pytest.mark.parametrize("how", ["own_post_rejected", "failed_run_then_another_ride"])
def test_qa_p1_put_it_back_survives_a_run_that_did_not_put_the_ride_back(env, freeze_clock, how):
    """The rider clicked "Put it back"; the next run did not re-date the
    ride (intervals.icu refused the ride's POST, or the run failed and the
    rider then saved another ride). The next good run must still put it
    back: nothing tells the rider their click was dropped."""
    _held_ride(env, day=10, to=11, window=DEFAULT)
    _own(env, put_back=[{"day": D(10), "icu_day": D(11)}])
    freeze_clock(NOON + timedelta(minutes=2))
    if how == "own_post_rejected":
        env.fk.reject_fileless = True
        assert {"day": D(10), "reason": "own_rejected"} in _push(env)["skipped"]
        env.fk.reject_fileless = False
    else:
        env.fk.fail["GET"] = (0, b"timed out")
        assert _push(env, ok=False)["error"] == "network"
        env.fk.fail.clear()
        freeze_clock(NOON + timedelta(minutes=3))
        _own(env, set=[_ride(3, name="Spin", minutes=60)])
    assert env.fk.where(_own_id(env, 10)) == [D(11)]      # not back yet
    freeze_clock(NOON + timedelta(minutes=6))
    _push(env)
    assert env.fk.where(_own_id(env, 10)) == [D(10)], "the rider's Put it back was dropped"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "QA-INT S1: an earlier ride's lasting moved_from makes 'Move it in my plan "
    "too' keep the old event held: the ride twice on its new day"))
def test_qa_s1_following_a_second_ride_from_a_day_an_earlier_ride_left_leaves_one_event(env):
    """Ride 1 was moved in Domestique from day 30 to day 31 (long done: its
    old event is gone). Later ride 2 is added on day 30, the rider drags it
    in intervals.icu to day 33 and answers "Move it in my plan too". Day 33
    must hold ride 2 once."""
    _write(_plan())
    _sync_on(env)
    _window(env, WIDE)
    _own(env, set=[_ride(30, name="Club ride")])
    _push(env)
    _own(env, move=[{"from": D(30), "to": D(31)}])
    _push(env)
    assert _ours(env).get(_own_id(env, 31)) == [D(31)] and _own_id(env, 30) not in _ours(env)
    _own(env, set=[_ride(30, name="Evening spin", minutes=60)])
    _push(env)
    icp._RECENT_PUSH.clear()
    env.fk.drag(_own_id(env, 30), D(33))
    _push(env)
    assert _status(env)["own_moved"] == [{"day": D(30), "icu_day": D(33)}]
    _own(env, move=[{"from": D(30), "to": D(33)}])        # "Move it in my plan too"
    _push(env)
    on_33 = sorted(e for e, ds in _ours(env).items() if D(33) in ds)
    assert on_33 == [_own_id(env, 33)], f"the evening spin twice on {D(33)}: {on_33}"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "QA-INT S2: a ride removed while its event is held stays in intervals.icu, "
    "held for good and no longer reported to the rider"))
def test_qa_s2_a_ride_removed_while_its_event_is_held_is_not_left_behind_unmentioned(env):
    """The rider dragged their pushed ride in intervals.icu, then removed the
    ride in Domestique. Its event must not stay in intervals.icu with
    nothing in Domestique ever mentioning it again."""
    _held_ride(env, day=10, to=11, window=DEFAULT)
    _own(env, remove=[D(10)])
    _push(env)
    _push(env)
    left = _ours(env).get(_own_id(env, 10))
    asked = _status(env)["own_moved"]
    assert left is None or asked, f"the removed ride stays on {left}, and nothing asks about it"


def test_m8_linking_a_ride_to_its_intervals_icu_ride_leaves_one_ride_on_the_day(env):
    _write(_plan())
    _own(env, set=[_ride(12)])
    _push(env)
    assert _ours(env).get(_own_id(env, 12)) == [D(12)]
    rider = env.fk.rider_event(D(12), eid=9002)
    lst, x = _listed(env, rider)
    assert x["status"] == "day_has_your_ride"
    _own(env, profile_id=lst["profile_id"], set=[_import_item(x)])
    _push(env)
    assert [e.get("id") for e in env.fk.events if e["start_date_local"][:10] == D(12)] == [rider]
    assert _day(_read(), 12)["own"]["source"] == "icu"
    _assert_mirror(env, DEFAULT)


def test_c6_send_refuses_a_day_holding_an_own_ride_inside_and_beyond_the_window(env):
    _write(_plan())
    _own(env, set=[_ride(10), _ride(26)])
    mark = len(env.fk.calls)
    for i in (10, 26):
        assert _send(env, i).get("error") == "own_ride_day"
    assert not [c for c in env.fk.calls[mark:] if c[0] in ("POST", "PUT")]


def test_x9_a_send_replaced_by_an_own_ride_beyond_the_window_goes_once_the_window_reaches_it(env, freeze_clock):
    """Grill X9 (documented): at 2 weeks the rider Sends day 20's workout,
    then adds a ride of their own on day 20. The Sent workout stays until
    the window reaches day 20; then it goes, and the ride is there once."""
    _write(_plan())
    _push(env)
    assert _send(env, 20).get("ok") and _ours(env).get(_eid(env, 20)) == [D(20)]
    _own(env, set=[_ride(20)])
    _push(env)
    assert _ours(env).get(_eid(env, 20)) == [D(20)] and _own_id(env, 20) not in _ours(env)
    freeze_clock(NOON + timedelta(days=20 - DEFAULT))
    _push(env)
    assert _eid(env, 20) not in _ours(env) and _ours(env).get(_own_id(env, 20)) == [D(20)]
    _assert_mirror(env, DEFAULT)


def test_o7_a_ride_on_the_test_day_moves_the_test_and_the_calendar_follows_once(env):
    """O7 × the push: the FTP test moves to a rested day (the endpoint says
    where); the calendar then holds the test once, on its new day, and the
    ride on the old one."""
    _write(_plan(rows={9: _sess(D(9), zwo="w09.zwo", stype="ftp_test", tss_estimate=60.0)}))
    _push(env)
    j = _own(env, set=[_ride(9, minutes=120)])
    (moved,) = j["ftp_test_moved"]
    assert moved["from"] == D(9) and moved["to"] > D(9) and moved["text"]
    _push(env)
    test = _day(_read(), moved["to"])
    assert test["session_type"] == "ftp_test"
    assert [s["day"] for w in _read()["weeks"] for s in w["sessions"]
            if s["session_type"] == "ftp_test"] == [moved["to"]]
    assert [e["start_date_local"][:10] for e in env.fk.events
            if e.get("filename") == test["zwo_file"]] == [moved["to"]]
    assert _ours(env).get(_own_id(env, 9)) == [D(9)]
    _assert_mirror(env, DEFAULT)


def test_c5_an_imported_ride_moved_in_intervals_icu_is_followed_on_request_and_nothing_is_sent(env):
    """The import list shows an imported ride the rider moved there as
    "moved"; following it moves the plan's ride, gives the old day its
    workout back in the calendar, and sends nothing for the ride itself."""
    _write(_plan())
    rider = env.fk.rider_event(D(12), eid=9001)
    lst, x = _listed(env, rider)
    _own(env, profile_id=lst["profile_id"], set=[_import_item(x)])
    _push(env)
    next(e for e in env.fk.events if e.get("id") == rider)["start_date_local"] = f"{D(13)}T00:00:00"
    lst, x = _listed(env, rider)
    assert (x["status"], x["plan_day"], x["date"]) == ("moved", D(12), D(13))
    _own(env, profile_id=lst["profile_id"],
         move=[{"from": x["plan_day"], "to": x["date"], "icu_day": x["date"]}])
    mark = len(env.fk.calls)
    _push(env)
    posted, deleted = _touched(env, mark)
    assert not [e for e in posted | deleted if e and e.endswith(":own")]
    assert _day(_read(), 13)["own"]["icu_event_id"] == rider and not _day(_read(), 12).get("own")
    assert next(e for e in env.fk.events if e.get("id") == rider)["start_date_local"][:10] == D(13)
    _assert_mirror(env, DEFAULT)


def test_c1_a_ride_over_a_workout_the_rider_moved_in_intervals_icu_leaves_that_workout_held(env):
    """C1's decided exception (#28 status quo), pinned: the workout the
    rider had moved in intervals.icu stays where they put it, the ride goes
    out under its own id, nothing is doubled."""
    _write(_plan(hard=(2,)))
    _drag_wednesday_onto_tuesday(env)
    _own(env, set=[_ride(2)])
    _push(env)
    assert env.fk.where(_eid(env, 2)) == [D(1)]
    assert _ours(env).get(_own_id(env, 2)) == [D(2)]
    _assert_mirror(env, DEFAULT, moved={_eid(env, 2): D(1)})


# ═══ B. C9 end to end: a workout held in intervals.icu is never eased ═══════

def _drag_wednesday_onto_tuesday(env):
    """Week 1 pushed; the rider drags Wednesday's VO2max onto Tuesday's Z2
    in intervals.icu (onto a training day: never learned, held). The run
    reports the held plan day."""
    _push(env)
    icp._RECENT_PUSH.clear()
    env.fk.drag(_eid(env, 2), D(1))
    r = _push(env)
    assert r["held"] == 1 and not r.get("learned")
    assert _status(env)["last_result"]["held_days"] == [D(2)]


@pytest.mark.parametrize("moved", [True, False], ids=["moved-in-intervals.icu", "not-moved"])
def test_c9_a_workout_moved_in_intervals_icu_is_never_eased_by_a_hard_own_ride(env, moved):
    """C9: a hard club ride on Thursday next to Wednesday's VO2max. Moved in
    intervals.icu, the VO2max keeps its file and stays where the rider put
    it, with a plain warning; not moved, it is eased (the control)."""
    _write(_plan(hard=(2,)))
    if moved:
        _drag_wednesday_onto_tuesday(env)
    j = _own(env, set=[_ride(3, intensity="hard")])
    wed = _day(_read(), 2)
    if not moved:
        assert wed["session_type"] != "vo2max" and wed.get("own_eased_from")
        return
    assert (wed["session_type"], wed["zwo_file"]) == ("vo2max", "w02.zwo")
    assert "pinned_next_to_ride" in [w["code"] for w in j["warnings"]]
    _push(env)
    assert env.fk.where(_eid(env, 2)) == [D(1)]
    _assert_mirror(env, DEFAULT, moved={_eid(env, 2): D(1)})


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "QA-INT C9b: held_days live only in the last calendar result; a failed run "
    "or the other rider's run erases them, so the move is eased and pulled back"))
@pytest.mark.parametrize("then", ["a_failed_run", "the_other_riders_run"])
def test_qa_c9b_a_later_run_that_did_not_see_the_calendar_keeps_the_move_pinned(env, then):
    """Nothing changed in intervals.icu since the run that saw the move: a
    later run failed (network), or the other rider on this install pressed
    Push in their profile. A hard ride added next to the moved VO2max must
    still leave it alone, and the next push must leave it where the rider
    put it."""
    _write(_plan(hard=(2,)))
    _drag_wednesday_onto_tuesday(env)
    if then == "a_failed_run":
        env.fk.fail["GET"] = (0, b"timed out")
        assert _push(env, ok=False)["error"] == "network"
        env.fk.fail.clear()
    else:
        pid_b = _other_rider(env)
        env.pm.switch(pid_b)
        _push(env)
        env.pm.switch(env.pid)
        db.shutdown_sync()
    _own(env, set=[_ride(3, intensity="hard")])
    assert _day(_read(), 2)["session_type"] == "vo2max", \
        "the VO2max the rider moved in intervals.icu was eased"
    _push(env)
    assert env.fk.where(_eid(env, 2)) == [D(1)], "the rider's move was pulled back"


# ═══ C. F1 × F2: an imported check-in never rewrites an own ride or a race ══

def _today_row(kind):
    s = _sess(D(0), zwo="", stype="tempo", duration_min=120, tss_estimate=100.0)
    if kind == "race":
        # _mark_race_days types a race "recovery", which no check-in gate
        # touches; a tempo-typed race row proves the guard, not the type.
        s.update(is_race=True, description="🏁 B RACE: Club TT",
                 race={"name": "Club TT", "priority": "B", "km": 25, "climb_m": 0})
    return s


@pytest.mark.parametrize("record", list(CHECK_INS))
@pytest.mark.parametrize("kind", ["own_ride", "race", "planned_session"])
def test_i5_x1_an_imported_check_in_never_rewrites_todays_own_ride_or_race(env, kind, record):
    """I5 / X1 / C13: intervals.icu's check-in (sore legs; the fixed rest
    limit of the band; an injury) eases a planned session (the control) but
    never today's own ride or race: Today shows it as planned, the own ride
    with a plain advisory line, and Accept is refused."""
    _write(_plan(rows={0: _today_row(kind)}))
    if kind == "own_ride":
        _own(env, set=[_ride(0, intensity="moderate", minutes=120)])
    _import_check_in(CHECK_INS[record]())
    assert env.client.get("/api/readiness").json()["checkin_imported"] is True
    if record == "fixed_rest_limit":                      # only the band acts
        dl = env.client.get("/api/daily-log").json()
        assert dl["band"]["band"] == "rest" and dl["today"]["soreness"] < NEG
    before = _read()
    j = env.client.get("/api/today-session").json()
    if kind == "planned_session":
        assert j["was_modified"] is True and j["adjusted"]["session_type"] != "tempo"
        return
    assert j["was_modified"] is False and j["reason"] == ""
    assert (j["adjusted"]["session_type"], j["adjusted"]["duration_min"]) == (
        j["planned"]["session_type"], j["planned"]["duration_min"])
    if kind == "own_ride":
        assert j["own"]["name"] == "Club ride" and j["own_advisory"]
    else:
        assert j["is_race"] is True and not j["own_advisory"]
    r = env.client.post("/api/today-session/persist", json={
        "session_type": "recovery", "duration_min": 30, "reason": "check-in"})
    assert r.status_code == 400 and _read() == before


@pytest.mark.parametrize("record", ["soreness_gate", "fixed_rest_limit"])
def test_i6_c14_auto_adjust_and_ease_today_leave_an_own_ride_as_the_rider_set_it(env, record):
    """I6 / C14: with the severity coming from the imported check-in (no
    override), auto-adjust for today and Ease today refuse the own ride with
    a plain line; the week walk leaves it alone and eases the week's
    VO2max (tier-down), or refuses (rest collapses to today)."""
    _write(_plan(hard=(2,)))
    _own(env, set=[_ride(0, intensity="moderate", minutes=120),
                   _ride(5, name="Saturday bunch", intensity="hard")])
    _import_check_in(CHECK_INS[record]())
    sev = env.client.get("/api/readiness").json()["severity"]
    assert sev in ("tier_down", "rest"), sev
    before = _read()
    for path, body in (("/api/plan/auto-adjust", {"scope": "today"}),
                       ("/api/readiness/apply-tier-down", {"date": D(0)})):
        r = env.client.post(path, json=body)
        assert r.status_code == 400 and "your own ride" in r.json()["error"], (path, r.text[:300])
        assert _read() == before
    r = env.client.post("/api/plan/auto-adjust", json={"scope": "week"})
    after = _read()
    assert _day(after, 0) == _day(before, 0) and _day(after, 5) == _day(before, 5)
    if sev == "tier_down":
        assert r.status_code == 200 and _day(after, 2)["session_type"] != "vo2max", r.text[:300]
    else:
        assert r.status_code == 400


def test_i6_a_race_day_stays_the_race_whatever_the_imported_check_in_says(env):
    race = _sess(D(0), zwo="", stype="recovery", duration_min=60, tss_estimate=60.0,
                 is_race=True, description="🏁 B RACE: Club TT",
                 race={"name": "Club TT", "priority": "B", "km": 25, "climb_m": 0})
    _write(_plan(hard=(2,), rows={0: race}))
    _import_check_in(_fixed_rest_limit())
    before = _day(_read(), 0)
    for path, body in (("/api/plan/auto-adjust", {"scope": "today"}),
                       ("/api/plan/auto-adjust", {"scope": "week"}),
                       ("/api/readiness/apply-tier-down", {"date": D(0)})):
        env.client.post(path, json=body)
        assert _day(_read(), 0) == before, path
    j = env.client.get("/api/today-session").json()
    assert j["was_modified"] is False and j["is_race"] is True


def test_i6_apply_rest_day_for_tomorrow_refuses_an_own_ride_from_an_imported_check_in(env):
    """The readiness card's "Apply rest day" (scope day = tomorrow) with the
    rest severity from intervals.icu's check-in: tomorrow's own ride stays."""
    _write(_plan())
    _own(env, set=[_ride(1, intensity="moderate", minutes=120)])
    _import_check_in(_fixed_rest_limit())
    assert env.client.get("/api/readiness").json()["severity"] == "rest"
    before = _read()
    r = env.client.post("/api/plan/auto-adjust", json={"scope": "day"})
    assert r.status_code == 400 and "your own ride" in r.json()["error"], r.text[:300]
    assert _read() == before


# ═══ D. two riders on one install (AC1) ═════════════════════════════════════

def _rider_a(env):
    """Rider A: 6 weeks, sync on, a ride of their own, weight followed from
    intervals.icu."""
    _write(_plan())
    _sync_on(env)
    _window(env, WIDE)
    _own(env, set=[_ride(30)])
    r = env.client.post("/api/settings", json={"weight_follow_icu": True})
    assert r.status_code == 200, r.text[:300]
    assert env.pm._athlete.get("weight_source") == "icu"


@pytest.mark.parametrize("reset_stop", [False, True], ids=["stop-flag", "snapshot-mismatch"])
def test_i8_a_switch_mid_sync_puts_none_of_a_into_b(env, monkeypatch, reset_stop):
    """I8 (mid-sync): A's wellness pass (a check-in and a scale weight) is
    abandoned when the switch to B lands while it fetches: B gets no
    check-in, no weight, nothing of A's window or rides; A's next pass lands
    in A."""
    _rider_a(env)
    pid_b = _other_rider(env)
    b_dir = env.pm._profiles_dir / pid_b
    b_athlete = (b_dir / "athlete.json").read_text()
    b_metrics = _rows(b_dir / "health_tracker.db", "athlete_metrics")
    a_weight = env.pm._athlete.get("weight_kg")
    rec = {"id": D(0), "ctl": 40.0, "atl": 38.0, **_soreness_gate(), "weight": 72.4}
    assert a_weight is None or abs(float(a_weight) - 72.4) >= db.WEIGHT_FOLLOW_MIN_CHANGE_KG

    def fetch_and_switch(days=90):
        env.pm.switch(pid_b)
        if reset_stop:
            db.shutdown_sync()
        return [rec]

    monkeypatch.setattr(db, "fetch_wellness", fetch_and_switch)
    with pytest.raises(db.SyncAborted):
        db.sync_wellness(days=1)
    db.shutdown_sync()
    assert _rows(b_dir / "health_tracker.db", "daily_log") == 0
    assert _rows(b_dir / "health_tracker.db", "athlete_metrics") == b_metrics
    assert (b_dir / "athlete.json").read_text() == b_athlete
    assert not (env.client.get("/api/daily-log").json() or {}).get("today")
    assert env.client.get("/api/readiness").json()["checkin_imported"] is False
    st = _status(env)
    assert (st["horizon_days"], st["own_moved"], st["sync_enabled"]) == (DEFAULT, [], False)
    assert not any(s.get("own") for w in _read()["weeks"] for s in w["sessions"])
    env.pm.switch(env.pid)
    db.shutdown_sync()
    monkeypatch.setattr(db, "fetch_wellness", lambda days=90: [rec])
    assert db.sync_wellness(days=1) == 1
    assert env.client.get("/api/readiness").json()["checkin_imported"] is True
    assert round(float(env.pm._athlete["weight_kg"]), 1) == 72.4
    assert _rows(b_dir / "health_tracker.db", "daily_log") == 0


@pytest.mark.parametrize("trigger", ["window_setting", "plan_write", "daily_run"])
def test_i8_a_calendar_run_that_waited_through_a_switch_never_reaches_b(env, monkeypatch, trigger):
    """I8 (mid-reconcile): A's run waits for the calendar lock; the switch to
    B lands meanwhile. Nothing reaches B's calendar, B's profile gets no
    clean-up record, A's record stays in A's, and B's status shows none of
    A's window, moves or result."""
    cals = Calendars()
    monkeypatch.setattr(icp, "_http", cals)
    env.fk = cals.cal("i123")
    _rider_a(env)
    pid_b = _other_rider(env)
    assert json.loads(_state_path(env, env.pid).read_text())["pushed_until"] == D(WIDE)
    arrived, out = _spy_lock_wait(monkeypatch), {}
    run = {"window_setting": lambda: out.setdefault(
               "r", app_module.api_icu_push({"horizon_days": icp.HORIZON_CHOICES[1]})),
           "plan_write": lambda: app_module._icu_push_debounced_run(env.pid),
           "daily_run": app_module._icu_push_daily_from_sync}[trigger]
    assert icp._RECONCILE_LOCK.acquire(timeout=1)
    try:
        t = threading.Thread(target=run)
        t.start()
        assert arrived.wait(5)
        env.pm.switch(pid_b)
    finally:
        icp._RECONCILE_LOCK.release()
    t.join(10)
    assert not t.is_alive()
    if trigger == "window_setting":
        assert out["r"]["error"] == "profile_changed"
    assert not [c for c in cals.calls if c[0] == "i456"]
    assert not _state_path(env, pid_b).exists()
    assert json.loads(_state_path(env, env.pid).read_text())["pushed_until"] == D(WIDE)
    st = _status(env)
    assert (st["horizon_days"], st["own_moved"], st["last_result"]) == (DEFAULT, [], None)


def test_i8_a_switch_while_a_run_reads_the_calendar_keeps_its_result_with_its_rider(env, monkeypatch, freeze_clock):
    """The switch lands while A's run is reading the calendar (the accepted
    seconds-wide race): A's run stays A's. B's plan (with a put-back of B's
    own pending) is not touched by A's result, B gets no record, and B's
    status shows nothing of A's."""
    cals = Calendars()
    monkeypatch.setattr(icp, "_http", cals)
    env.fk = cals.cal("i123")
    _rider_a(env)
    pid_b = _other_rider(env)
    env.pm.switch(pid_b)
    db.shutdown_sync()
    _own(env, set=[_ride(10)])
    _own(env, put_back=[{"day": D(10), "icu_day": D(11)}])
    b_plan = _read()
    env.pm.switch(env.pid)
    db.shutdown_sync()
    freeze_clock(NOON + timedelta(minutes=2))
    cals.on_get = lambda: env.pm.switch(pid_b)
    j = _push(env)
    assert j["profile_id"] == env.pid
    assert _read() == b_plan                              # B's put-back still stands
    assert not _state_path(env, pid_b).exists()
    st = _status(env)
    assert (st["horizon_days"], st["own_moved"], st["last_result"]) == (DEFAULT, [], None)
    assert not [c for c in cals.calls if c[0] == "i456"]


@pytest.mark.parametrize("import_breaks", [False, True], ids=["check-in-imported", "import-fails"])
def test_i9_a_sync_pass_with_a_check_in_still_rolls_the_calendar_in_the_riders_window(env, monkeypatch, import_breaks):
    """I9: one background sync pass that brings a check-in (or whose import
    breaks on a malformed answer) is followed by the daily calendar run in
    the rider's own 6 weeks, own ride included."""
    _write(_plan())
    _sync_on(env)
    env.pm.save_prefs({icp._HORIZON_PREF: WIDE})
    _own(env, set=[_ride(30)])
    rec = {"id": D(0), "ctl": 40.0, "atl": 38.0, **_soreness_gate()}
    monkeypatch.setattr(db, "fetch_wellness", lambda days=90: [rec])
    monkeypatch.setattr(db, "fetch_activities", lambda days=90: [])
    if import_breaks:
        def _broken(_record):
            raise ValueError("malformed answer")
        monkeypatch.setattr(db, "icu_check_in", _broken)
    res = db.run_sync(days=1)
    assert res["status"] == "ok" and res["wellness"] == 1, res
    app_module._after_background_sync()                   # what the sync loop runs next
    gets = [c[1] for c in env.fk.calls if c[0] == "GET"]
    assert gets == [f"athlete/i123/events?oldest={D(-7)}&newest={D(WIDE + LOOK)}"]
    assert _ours(env).get(_own_id(env, 30)) == [D(30)]
    _assert_mirror(env, WIDE)
    assert env.client.get("/api/readiness").json()["checkin_imported"] is (not import_breaks)


# ═══ E. a rebuild and a new plan keep own rides; one event each after ══════

GEN = {"goal": "ftp", "hours_per_week": 8.0, "max_weekday": 2.0, "max_weekend": 4.0,
       "plan_weeks": 8, "rest_days": [0], "daily_availability": {}}


def _free_day(plan, after):
    """The first day from ``after`` on with one pending planned session that
    has a workout file and is no FTP test, nor next to one."""
    by_day: dict = {}
    for w in plan["weeks"]:
        for s in w["sessions"]:
            by_day.setdefault(s["day"], []).append(s)
    tests = {s["day"] for ss in by_day.values() for s in ss if s["session_type"] == "ftp_test"}
    for day in sorted(by_day):
        ss = by_day[day]
        near = {(date.fromisoformat(day) + timedelta(days=k)).isoformat() for k in (-1, 0, 1)}
        if (day >= D(after) and len(ss) == 1 and ss[0]["session_type"] != "rest"
                and ss[0].get("zwo_file") and not ss[0].get("own") and not ss[0].get("is_race")
                and (ss[0].get("status") or "pending") == "pending" and not near & tests):
            return day
    raise AssertionError("no free day")


def test_e1_a_rebuild_and_a_new_plan_keep_own_rides_and_the_push_after_leaves_one_event_each(env):
    r = env.client.post("/api/plan/generate", json=GEN)
    assert r.status_code == 200, r.text[:300]
    _window(env, WIDE)
    plan = _read()
    manual_day = _free_day(plan, 8)
    imported_day = _free_day(plan, 16)
    rider = env.fk.rider_event(imported_day, eid=9100)
    lst, x = _listed(env, rider)
    j = _own(env, profile_id=lst["profile_id"], set=[
        {"date": manual_day, "name": "Club ride", "duration_min": 120, "intensity": "easy"},
        _import_item(x)])
    assert sorted(a["date"] for a in j["applied"]) == sorted([manual_day, imported_day])
    mark = len(env.fk.calls)
    _push(env)
    _assert_mirror(env, WIDE)

    def kept():
        own = {s["day"]: s["own"] for w in _read()["weeks"] for s in w["sessions"] if s.get("own")}
        assert (own[manual_day]["source"], own[manual_day]["name"]) == ("manual", "Club ride")
        assert (own[imported_day]["source"], own[imported_day]["icu_event_id"]) == ("icu", rider)

    r = env.client.post("/api/plan/regenerate", json={})
    assert r.status_code == 200, r.text[:300]
    assert not (r.json().get("regen_info") or {}).get("own_dropped")
    kept()
    _push(env)
    _assert_mirror(env, WIDE)
    r = env.client.post("/api/plan/generate", json=GEN)
    assert r.status_code == 200, r.text[:300]
    kept()
    _push(env)
    _assert_mirror(env, WIDE)
    posted, deleted = _touched(env, mark)
    assert f"domestique:{env.pid}:{imported_day}:own" not in posted | deleted
    assert next(e for e in env.fk.events if e.get("id") == rider)["start_date_local"][:10] == imported_day


def test_e2_a_race_added_on_an_own_ride_day_takes_the_day_and_the_rides_event_goes(env):
    """B18 × the push: a race added (add-race rebuilds the plan) on the day
    of a pushed own ride wins the day; the next push removes the ride's
    event and sends nothing for the race."""
    r = env.client.post("/api/plan/generate", json=GEN)
    assert r.status_code == 200, r.text[:300]
    _window(env, WIDE)
    day = _free_day(_read(), 10)
    _own(env, set=[{"date": day, "name": "Club ride", "duration_min": 120, "intensity": "easy"}])
    _push(env)
    assert _ours(env).get(f"domestique:{env.pid}:{day}:own") == [day]
    r = env.client.post("/api/plan/add-race", json={"date": day, "priority": "B",
                                                    "name": "Club TT", "km": 25})
    assert r.status_code == 200, r.text[:300]
    row = _day(_read(), day)
    assert row.get("is_race") and not row.get("own")
    _push(env)
    assert not [e for e, ds in _ours(env).items() if day in ds]
    _assert_mirror(env, WIDE)


# ═══ F. the shrink 6 → 2 weeks with own rides and Send pushes beyond it ═════

def test_f3_a_pending_clean_up_removes_own_rides_beyond_and_spares_a_send_made_meanwhile(env):
    """H1 × C4 × C6: the shrink's clean-up fails once (still pending); the
    rider Sends day 26's workout meanwhile (and cannot Send on the own-ride
    day 30). The retry removes the ride on day 30 and the other far workouts,
    keeps the Send and the ride inside the window."""
    _write(_plan())
    _sync_on(env)
    _window(env, WIDE)
    _own(env, set=[_ride(10), _ride(30)])
    _push(env)
    assert {_own_id(env, 10), _own_id(env, 30)} <= set(_ours(env))
    env.fk.fail["PUT"] = (500, b"try later")
    assert _window(env, DEFAULT)["error"] == "sweep_failed"
    assert _own_id(env, 30) in _ours(env)
    assert _send(env, 26).get("ok")
    assert _send(env, 30).get("error") == "own_ride_day"
    env.fk.fail.clear()
    _push(env)
    ours = _ours(env)
    assert ours.get(_eid(env, 26)) == [D(26)]
    assert _own_id(env, 30) not in ours and ours.get(_own_id(env, 10)) == [D(10)]
    assert not [e for e, ds in ours.items() if e != _eid(env, 26) and ds[0] > D(DEFAULT)]
    _push(env)
    assert _ours(env).get(_eid(env, 26)) == [D(26)]


def test_f3_after_a_clean_shrink_a_send_beyond_the_window_stays(env):
    _write(_plan())
    _sync_on(env)
    _window(env, WIDE)
    _own(env, set=[_ride(30)])
    _push(env)
    _window(env, DEFAULT)
    assert _own_id(env, 30) not in _ours(env)
    assert _send(env, 26).get("ok")
    for _ in range(2):
        _push(env)
        assert _ours(env).get(_eid(env, 26)) == [D(26)]


# ═══ H. disconnect, where all three meet ════════════════════════════════════

def test_disconnect_removes_what_domestique_put_there_and_imported_and_keeps_the_riders_own(env):
    """Disconnect (G-A × C4 × grill_wellness H1/M6): our workouts and rides
    go from the 6-week calendar, the rider's own event stays; the imported
    check-in goes, the rider's own check-in stays; the followed weight stays
    as the rider's; the plan keeps its own rides."""
    _write(_plan())
    _sync_on(env)
    _window(env, WIDE)
    _own(env, set=[_ride(10), _ride(30)])
    _push(env)
    rider = env.fk.rider_event(D(12), eid=9001)
    assert env.client.post("/api/settings", json={"weight_follow_icu": True}).status_code == 200
    db.upsert_daily_log(D(-1), NORMAL, NORMAL, NORMAL, NORMAL, NORMAL)   # the rider's, yesterday
    _import_check_in({**_soreness_gate(), "weight": 72.4})
    assert round(float(env.pm._athlete["weight_kg"]), 1) == 72.4
    plan = _read()
    r = env.client.post("/api/icu/disconnect")
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    assert _ours(env) == {} and [e.get("id") for e in env.fk.events] == [rider]
    rows = dict(db.get_db().execute("SELECT date, icu_fields FROM daily_log").fetchall())
    assert rows == {D(-1): None}
    assert round(float(env.pm._athlete["weight_kg"]), 1) == 72.4
    assert env.pm._athlete.get("weight_source") == "manual"
    assert _read() == plan


# ═══ G. I10: the UI where the features meet ═════════════════════════════════

def test_i10_the_calendar_sync_text_names_no_fixed_window():
    src = DASHBOARD.read_text(encoding="utf-8")
    i = src.index('id="icu-cal-sync-block"')
    block = src[i:src.index("IMPL-HRV-RECOVERY", i)]
    text = " ".join(block.split()).lower()
    for phrase in ("14 days", "two weeks", "next 2 weeks", "fortnight"):
        assert phrase not in text, phrase


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("own_today", [True, False])
def test_i10_ease_today_is_not_offered_on_an_own_ride_day(own_today):
    src = DASHBOARD.read_text(encoding="utf-8")
    fn = _extract_js_function(src, "loadReadinessComposite")
    flag = "true" if own_today else "false"
    harness = """
const host = {innerHTML: ''};
const document = {getElementById: id => id === 'today-verdict' ? host : null};
const window = {_todayIsOwn: %s};
function esc(s) { return String(s); }
async function getResource(url) {
  return {ok: true, json: async () => ({severity: 'tier_down', source: 'hooper',
          checkin_imported: true, severity_reasons: ['Your check-in'], score_0_100: 55})};
}
%s
loadReadinessComposite().then(() => {
  const ease = host.innerHTML.includes('apply-tier-down-btn');
  if (ease === %s) throw new Error('Ease today offered: ' + ease);
  if (!host.innerHTML.includes('apply-week-tier-down-btn')) throw new Error('week button gone');
  if (!host.innerHTML.includes('From your intervals.icu check-in')) throw new Error('no source pill');
  console.log('OK');
});
""" % (flag, fn, flag)
    _run_node(harness)
