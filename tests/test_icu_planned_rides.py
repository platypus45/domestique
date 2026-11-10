"""3.14.0 F2 — the rider's own planned rides from the intervals.icu calendar.

GET /api/icu/planned-rides lists cycling WORKOUT events from today on that
are not ours, with their length, load and how hard, and where each stands
against the plan; the dialog posts the ticked ones to POST
/api/plan/own-sessions. Read-only: nothing changes in intervals.icu.
Hermetic: intervals.icu only through a fake behind icu_calendar_push._http.
"""
from __future__ import annotations

import inspect
import json
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

import app as app_module
import icu_calendar_push as icp
import training_planner as tp
from test_icu_push import stub, _apikey  # noqa: F401  (stub is a fixture)
from test_331_surfaces import _extract_js_function

DASHBOARD = Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html"
TODAY = date(2026, 10, 19)          # a Monday


def _d(n):
    return (TODAY + timedelta(days=n)).isoformat()


class Fake:
    def __init__(self, rows=None, status=200, raw=None):
        self.rows, self.status, self.raw, self.calls = rows or [], status, raw, []

    def __call__(self, method, path, payload=None, timeout=30.0):
        self.calls.append((method, path))
        body = self.raw if self.raw is not None else json.dumps(self.rows).encode()
        return self.status, body


@pytest.fixture()
def env(stub, freeze_clock, monkeypatch):
    freeze_clock(TODAY)
    stub.fake = Fake()
    monkeypatch.setattr(icp, "_http", stub.fake)
    return stub


def _plan(weeks=2, rows=None):
    rows = rows or [("rest", 0, 0), ("z2", 60, 45), ("vo2max", 60, 75), ("z2", 90, 68),
                    ("rest", 0, 0), ("long_z2", 150, 112), ("z2", 90, 68)]
    out = {"goal": {"goal_type": "continuous"}, "weeks": []}
    for k in range(weeks):
        out["weeks"].append({
            "week_num": k + 1, "start": _d(7 * k), "end": _d(7 * k + 6), "phase": "base",
            "tss_target": 900, "is_stepback": False,
            "sessions": [{"day": _d(7 * k + i), "day_name": "", "session_type": st,
                          "duration_min": dur, "tss_estimate": tss, "description": st,
                          "status": "pending", "zwo_file": "", "zwo_name": ""}
                         for i, (st, dur, tss) in enumerate(rows)]})
    return out


def _write(p):
    pdir = Path(tp.PLAN_DIR)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "current_plan.json").write_text(json.dumps(p), encoding="utf-8")


def _read():
    return json.loads((Path(tp.PLAN_DIR) / "current_plan.json").read_text(encoding="utf-8"))


def _ev(eid, day, name="Club ride", cat="WORKOUT", **kw):
    return dict({"id": eid, "category": cat, "name": name, "type": "Ride",
                 "start_date_local": f"{day}T00:00:00"}, **kw)


def _get(env):
    return env.client.get("/api/icu/planned-rides").json()


def test_c1_l1_only_the_riders_own_cycling_rides_from_today_on(env):
    _apikey(env)
    _write(_plan())
    env.fake.rows = [
        _ev(5, _d(5), "Club ride", moving_time=10800, icu_training_load=190),
        _ev(6, _d(2), "Z2 from a coach", moving_time=3600),
        _ev(7, _d(3), "Run", type="Run", moving_time=3600),
        _ev(8, _d(4), "Our workout", external_id=f"domestique:{env.pid}:{_d(4)}:0"),
        _ev(9, _d(6), "Our own ride", external_id=f"domestique:{env.pid}:{_d(6)}:own"),   # T-P9
        _ev(10, _d(6), "Library push", external_id="domestique-manual:abc"),
        _ev(11, _d(8), "Hill race", cat="RACE_B"),
        _ev(12, _d(9), "Holiday", cat="NOTE"),
        _ev(13, _d(10), "A note", show_as_note=True),
        _ev(14, _d(10), "Week target", for_week=True),
        _ev(15, _d(-2), "Last week"),
        _ev(16, _d(11), "Hidden", hide_from_athlete=True),
        "not an event", _ev(True, _d(5), "bad id"),
    ]
    r = _get(env)
    assert [x["event_id"] for x in r["rides"]] == [6, 5]          # sorted by date
    assert r["profile_id"] == env.pid and r["gone"] == []
    (method, path), = env.fake.calls                              # one GET, read-only
    assert method == "GET" and f"oldest={TODAY.isoformat()}" in path


def test_c2_l1_length_load_and_how_hard(env):
    _apikey(env)
    _write(_plan())
    env.fake.rows = [
        _ev(1, _d(1), moving_time=10800, icu_training_load=240),          # 3 h, 240: hard
        _ev(2, _d(2), moving_time=7200, icu_intensity=67.0),              # 2 h at IF .67: easy
        _ev(3, _d(3), time_target=5400, load_target=98),                  # targets: moderate
        _ev(4, _d(5), name="No numbers"),
    ]
    rows = {x["event_id"]: x for x in _get(env)["rides"]}
    assert (rows[1]["duration_min"], rows[1]["load"], rows[1]["load_from"], rows[1]["intensity"]) == (180, 240, "icu", "hard")
    assert (rows[2]["duration_min"], rows[2]["load"], rows[2]["intensity"]) == (120, 90, "easy")
    assert (rows[3]["duration_min"], rows[3]["load"], rows[3]["intensity"]) == (90, 98, "moderate")
    assert (rows[4]["duration_min"], rows[4]["load"], rows[4]["load_from"], rows[4]["intensity"]) == (None, None, "intensity", "moderate")


def test_c3_where_each_ride_stands_against_the_plan(env):
    _apikey(env)
    p = _plan()
    p["weeks"][0]["sessions"][2]["is_race"] = True
    p["weeks"][0]["sessions"][1]["status"] = "done"
    _write(p)
    tp.apply_own_sessions(p, [
        {"date": _d(5), "name": "Imported", "duration_min": 120, "intensity": "moderate", "icu_event_id": 50},
        {"date": _d(6), "name": "Moved there", "duration_min": 60, "intensity": "easy", "icu_event_id": 51},
        {"date": _d(10), "name": "Deleted there", "duration_min": 60, "intensity": "easy", "icu_event_id": 52},
        {"date": _d(11), "name": "Manual", "duration_min": 60, "intensity": "easy"},
    ], [], library=[])
    _write(p)
    env.fake.rows = [_ev(50, _d(5)), _ev(51, _d(9)), _ev(60, _d(30), "After"),
                     _ev(61, _d(2), "On race"), _ev(62, _d(1), "Ridden"), _ev(63, _d(11), "Same as manual"),
                     _ev(64, _d(12), "New one")]
    r = _get(env)
    st = {x["event_id"]: (x["status"], x["plan_day"]) for x in r["rides"]}
    assert st == {50: ("added", _d(5)), 51: ("moved", _d(6)), 60: ("after_plan", None),
                  61: ("race_day", None), 62: ("ridden", None), 63: ("day_has_your_ride", None),
                  64: ("new", None)}
    assert r["gone"] == [{"event_id": 52, "date": _d(10), "name": "Deleted there"}]


@pytest.mark.parametrize("status,body,want", [
    (403, b'{"error":"insufficient scope CALENDAR:READ"}', {"needs_reconnect": True}),
    (401, b"", {"needs_reconnect": True}),
    (0, b"", {"error": "network"}),
    (500, b"", {"error": "http_500"}),
    (200, b"not json", {"error": "bad_response"}),
])
def test_c4_failures_are_plain_answers(env, status, body, want):
    _apikey(env)
    _write(_plan())
    env.fake.status, env.fake.raw = status, body
    assert _get(env) == want


def test_c4_not_connected(env):
    _write(_plan())
    r = _get(env)
    assert r.get("error") and not r.get("rides")


def test_l4_the_list_is_a_plain_def_so_its_blocking_read_runs_off_the_loop():
    assert not inspect.iscoroutinefunction(app_module.api_icu_planned_rides)


def test_c5_m1_adding_twice_is_refused_and_follow_the_move_is_one_change(env):
    _apikey(env)
    _write(_plan())
    env.fake.rows = [_ev(70, _d(5), moving_time=7200, icu_training_load=130)]
    r = _get(env)
    row = r["rides"][0]
    item = {"date": row["date"], "name": row["name"], "duration_min": row["duration_min"],
            "intensity": row["intensity"], "load": row["load"], "load_from": row["load_from"],
            "icu_event_id": row["event_id"], "icu_day": row["date"]}
    j = env.client.post("/api/plan/own-sessions", json={"profile_id": r["profile_id"], "set": [item]}).json()
    assert j["ok"] and j["applied"] == [{"action": "add", "date": _d(5)}]
    own = {s["day"]: s for w in _read()["weeks"] for s in w["sessions"]}[_d(5)]["own"]
    assert (own["source"], own["icu_event_id"], own["load_from"]) == ("icu", 70, "icu")
    again = env.client.post("/api/plan/own-sessions",
                            json={"profile_id": r["profile_id"], "set": [dict(item, date=_d(6))]})
    assert again.status_code == 400 and again.json()["refused"][0]["reason"] == "duplicate"
    # Moved in intervals.icu to Sunday: "Move it in my plan too" moves the ride and its day.
    env.fake.rows = [_ev(70, _d(6), moving_time=7200, icu_training_load=130)]
    r = _get(env)
    assert (r["rides"][0]["status"], r["rides"][0]["plan_day"]) == ("moved", _d(5))
    j = env.client.post("/api/plan/own-sessions", json={
        "profile_id": r["profile_id"], "move": [{"from": _d(5), "to": _d(6), "icu_day": _d(6)}]}).json()
    assert j["ok"]
    b = {s["day"]: s for w in _read()["weeks"] for s in w["sessions"]}
    assert b[_d(6)]["own"]["icu_day"] == _d(6) and not b[_d(5)].get("own")
    assert _get(env)["rides"][0]["status"] == "added"


# ═══ D. the dialogs (node harness) ═══════════════════════════════════════════

def _js(*names):
    src = DASHBOARD.read_text(encoding="utf-8")
    return "\n".join(_extract_js_function(src, n) for n in names)


def _run(harness):
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert "OK" in res.stdout, res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_d1_the_calendar_names_the_ride_not_no_workout_matched():
    _run(_js("calCardTitle", "calCardTitleWithStructure", "calStructureSuffix") + """
const CAL_CONTENT_LABEL = {}, CAL_SESSION_LABEL = {z2: 'Z2'};
const t = calCardTitleWithStructure({own: {name: 'Club ride'}, session_type: 'z2', zwo_file: '', name: 'Club ride'});
if (t !== 'Club ride') throw new Error(t);
const u = calCardTitle({session_type: 'z2', zwo_file: ''});
if (!/no workout matched/.test(u)) throw new Error(u);
console.log('OK');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_d2_an_unridden_own_day_opens_its_dialog_not_the_workout():
    _run(_js("calOpenDay") + """
let opened = null, workout = false;
function openOwnRideDialog(d) { opened = d; }
function _calOpenDayChooser() { workout = true; }
function _calShowMismatchBanner() { workout = true; }
function openActivityModal() { workout = true; }
globalThis.window = {_calData: {weeks: [{days: [
  {date: '2026-10-24', card_state: 'own', planned: {own: {name: 'Club ride'}, session_type: 'sweetspot', zwo_file: ''}, actual: null}
]}]}};
calOpenDay('2026-10-24').then(() => {
  if (opened !== '2026-10-24' || workout) throw new Error('opened ' + opened + ' workout ' + workout);
  console.log('OK');
});
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_d3_the_import_dialog_sends_the_ticked_rides_with_their_ids():
    _run(_js("_icuPlannedItems") + """
let _icuPlanned = {profile_id: 'p1', rides: [
  {event_id: 7, date: '2026-10-24', name: 'Club ride', duration_min: 180, load: 190, load_from: 'icu', intensity: 'hard', status: 'new'},
  {event_id: 8, date: '2026-10-25', name: 'No length', duration_min: null, load: null, load_from: 'intensity', intensity: 'moderate', status: 'day_has_your_ride'},
  {event_id: 9, date: '2026-10-26', name: 'Not ticked', duration_min: 60, load: null, intensity: 'easy', status: 'new'}]};
const boxes = [{checked: true, dataset: {i: '0'}}, {checked: true, dataset: {i: '1'}}, {checked: false, dataset: {i: '2'}}];
globalThis.document = {
  querySelectorAll: () => boxes,
  querySelector: (q) => q.includes('"1"') ? {value: '95'} : null,
  getElementById: (id) => id === 'own-imp-int-0' ? {value: 'moderate'} : null,
};
const items = _icuPlannedItems();
const want = JSON.stringify([
  {date: '2026-10-24', name: 'Club ride', icu_event_id: 7, icu_day: '2026-10-24', duration_min: 180, intensity: 'moderate', load: 190, load_from: 'icu'},
  {date: '2026-10-25', name: 'No length', icu_event_id: 8, icu_day: '2026-10-25', duration_min: 95, intensity: 'moderate', link: true}]);
if (JSON.stringify(items) !== want) throw new Error(JSON.stringify(items));
console.log('OK');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_d4_c6_send_is_never_offered_on_an_own_ride_and_its_day_takes_no_drop():
    src = DASHBOARD.read_text(encoding="utf-8")
    assert "(session.zwo_file && !session.own)" in src
    assert "!isOwn && !isPast" in _extract_js_function(src, "calDragStart")
