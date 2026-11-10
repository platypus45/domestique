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


# ═══ Wave 4 (3.14.0 QA fixes) ════════════════════════════════════════════════

def test_w4_qx_h2_a_ride_moved_in_domestique_is_not_moved_in_intervals_icu(env):
    _apikey(env)
    p = _plan()
    tp.apply_own_sessions(p, [{"date": _d(3), "name": "Saturday club ride", "duration_min": 120,
                               "intensity": "moderate", "icu_event_id": 53}], [], library=[])
    tp.apply_own_sessions(p, moves=[{"from": _d(3), "to": _d(4)}], library=[])   # in Domestique
    _write(p)
    env.fake.rows = [_ev(53, _d(3), "Saturday club ride")]                      # still there
    row = _get(env)["rides"][0]
    assert (row["status"], row["plan_day"]) == ("moved_here", _d(4))
    env.fake.rows = [_ev(53, _d(5), "Saturday club ride")]                      # now moved there
    assert _get(env)["rides"][0]["status"] == "moved"


def test_w4_l9_a_past_plan_day_offers_nothing(env, freeze_clock):
    _apikey(env)
    p = _plan()
    tp.apply_own_sessions(p, [{"date": _d(1), "name": "Tuesday ride", "duration_min": 60,
                               "intensity": "easy", "icu_event_id": 54}], [], library=[])
    _write(p)
    freeze_clock(TODAY + timedelta(days=2))
    env.fake.rows = [_ev(54, _d(5), "Tuesday ride")]
    assert _get(env)["rides"][0]["status"] == "in_plan_past"


def test_w4_qx_l12_an_estimated_load_says_so(env):
    _apikey(env)
    _write(_plan())
    env.fake.rows = [_ev(1, _d(1), moving_time=7200, icu_intensity=67.0),
                     _ev(2, _d(2), moving_time=7200, icu_training_load=150)]
    rows = {x["event_id"]: x for x in _get(env)["rides"]}
    assert rows[1]["load_estimated"] is True and rows[2]["load_estimated"] is False


_STUBS = """
const OWN_INTENSITY_LABEL = {easy: 'easy', moderate: 'moderate', hard: 'hard'};
const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
function _calTodayStr() { return '2026-10-19'; }
let MODAL = null, TOASTS = [];
function openModal(h) { MODAL = h; }
function closeModal() { MODAL = null; }
function showToast(m) { TOASTS.push(m); }
async function loadCalendar() {}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_w4_l8_qx_h4_qx_m9_the_dialog_posts_its_rider_asks_before_replacing_and_keeps_the_news():
    _run(_STUBS + _js("_ownDayLabel", "_ownResultText", "_ownResultView", "_ownPost",
                      "_ownReplaceConfirmed") + """
let BODIES = [], ANSWERS = [];
globalThis.window = {_ownDlg: {profile_id: 'rider-a'}};
const STATUS = {textContent: '', innerHTML: ''};
globalThis.document = {getElementById: (id) => id === 'own-status' ? STATUS : null};
globalThis.fetch = async (url, o) => { BODIES.push(JSON.parse(o.body)); const a = ANSWERS.shift();
  return {status: a[0], ok: a[0] === 200, json: async () => a[1]}; };
(async () => {
  ANSWERS.push([409, {ok: false, error: 'day_has_your_ride',
                      refused: [{reason: 'day_has_your_ride', name: 'Sunday club ride', date: '2026-10-25',
                                 text: 'There is already Sunday club ride on Sun 25 Oct.'}]}]);
  await _ownPost({move: [{from: '2026-10-24', to: '2026-10-25'}]}, STATUS);
  if (BODIES[0].profile_id !== 'rider-a') throw new Error('no profile id: ' + JSON.stringify(BODIES[0]));
  if (!/Replace it\\?/.test(STATUS.innerHTML) || !/Sunday club ride/.test(STATUS.innerHTML)) throw new Error(STATUS.innerHTML);
  ANSWERS.push([200, {ok: true, applied: [{action: 'move'}], shortened: [{text: 'Thu 22 Oct: your endurance ride is now 30 min (was 1 h 30) to keep the week\\'s load.'}]}]);
  await _ownReplaceConfirmed();
  if (!BODIES[1].move[0].replace) throw new Error('no replace: ' + JSON.stringify(BODIES[1]));
  if (!MODAL || !/your endurance ride is now 30 min/.test(MODAL) || !/Close/.test(MODAL)) throw new Error('news not kept: ' + MODAL);
  console.log('OK');
})().catch(e => { console.error(e); process.exit(1); });
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_w4_qx_m8_l9_qx_m4_the_dialog_for_a_past_ride_and_a_pending_put_back():
    _run(_STUBS + _js("_ownDayLabel", "_ownLen", "_ownCalDay", "_ownIntensitySelect",
                      "openOwnRideDialog") + """
let _profileData = {active: 'rider-a'};
const DAYS = [
  {date: '2026-10-16', planned: {own: {name: 'Thursday group ride', intensity: 'moderate', source: 'manual'}, duration_min: 90, tss: 98}, actual: null},
  {date: '2026-10-24', planned: {own: {name: 'Club ride', intensity: 'hard', source: 'manual', put_back_from: '2026-10-23'}, duration_min: 180, tss: 240}, actual: null},
];
globalThis.window = {_calData: {weeks: [{days: DAYS}]}};
globalThis.fetch = async () => ({json: async () => ({sync_enabled: true, connected: true, horizon_days: 14,
  own_moved: [{day: '2026-10-16', icu_day: '2026-10-20'}, {day: '2026-10-24', icu_day: '2026-10-23'}]})});
(async () => {
  await openOwnRideDialog('2026-10-16');
  if (/button class="btn btn-sm/.test(MODAL) || /type="date"/.test(MODAL) || !/Not ridden/.test(MODAL)) throw new Error('past: ' + MODAL);
  await openOwnRideDialog('2026-10-24');
  if (!/Going back to Sat 24 Oct at the next calendar update/.test(MODAL)) throw new Error('pending: ' + MODAL);
  if (!/disabled style="opacity:0.5;" onclick="ownRidePutBack/.test(MODAL)) throw new Error('put back not greyed: ' + MODAL);
  if (!/Move it in my plan too/.test(MODAL)) throw new Error('follow gone: ' + MODAL);
  if (window._ownDlg.profile_id !== 'rider-a') throw new Error('no rider');
  console.log('OK');
})().catch(e => { console.error(e); process.exit(1); });
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_w4_qx_h2_an_unchanged_load_is_not_sent_back():
    _run(_js("_ownFormItem") + """
const V = {'own-name': 'Club ride', 'own-h': '3', 'own-m': '0', 'own-int': 'hard', 'own-load': '190'};
globalThis.document = {getElementById: (id) => (id in V ? {value: V[id]} : null)};
globalThis.window = {_ownDlg: {own: {name: 'Club ride', load_from: 'icu'}, load0: '190'}};
let it = _ownFormItem();
if ('load' in it) throw new Error('unchanged load sent: ' + JSON.stringify(it));
V['own-load'] = '200';
it = _ownFormItem();
if (it.load !== 200) throw new Error('changed load not sent: ' + JSON.stringify(it));
console.log('OK');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_w4_qx_h1_the_grid_card_and_click_treat_own_rides_and_races_as_what_they_are():
    src = DASHBOARD.read_text(encoding="utf-8")
    grid = src[src.index("const cardState = s.card_state || (s.own ? 'own'"):]
    assert "own: s.own || null, is_race: !!s.is_race" in grid[:3000]
    assert "cardState !== 'own' && cardState !== 'race'" in grid[:6000]
    _run(_STUBS + _js("calCardTitle", "calCardTitleWithStructure", "calStructureSuffix",
                      "openOverviewDay", "openPlanDayWorkout") + """
const CAL_CONTENT_LABEL = {}, CAL_SESSION_LABEL = {recovery: 'RECOVERY'};
const race = calCardTitleWithStructure({is_race: true, race: {priority: 'A', name: 'Gran Fondo'}, session_type: 'recovery', zwo_file: ''});
if (race !== 'A race: Gran Fondo') throw new Error(race);
let opened = null, workoutDay = null;
function openOwnRideDialog(d) { opened = d; }
function calContentMatches() { return true; }
function calOpenDay() {}
function openDayWorkout(i) { workoutDay = window._weekPlanSessions[i].day; }
globalThis.window = {_calData: {weeks: []}, _planData: {weeks: [{start: '2026-10-20', sessions: [
  {day: '2026-10-21', own: {name: 'Wednesday chaingang'}, session_type: 'sweetspot'},
  {day: '2026-10-22', session_type: 'z2'}]}]}};
openOverviewDay(0, 2, '2026-10-21');
if (opened !== '2026-10-21') throw new Error('own dialog not opened');
openOverviewDay(0, 3, '2026-10-22');          // the Thursday column of a Tuesday week
if (workoutDay !== '2026-10-22') throw new Error('wrong day: ' + workoutDay);
console.log('OK');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_w4_qx_m3_l5_a_moved_ride_is_marked_and_lost_rides_are_said():
    _run(_STUBS + _js("_ownDayLabel", "_ownMovedLine", "_ownLostNotice") + """
globalThis.window = {_ownMoved: {'2026-10-24': '2026-10-23'}};
const m = _ownMovedLine('2026-10-24');
if (m !== 'moved in intervals.icu to Fri 23 Oct: open it to choose') throw new Error(m);
if (_ownMovedLine('2026-10-25') !== '') throw new Error('marked a day not moved');
const lines = _ownLostNotice({own_lost: ['Your ride Far ride on Thu 3 Dec could not come along into the new plan: That day is not in your plan.']});
if (lines.length !== 1 || !/Far ride/.test(TOASTS[0])) throw new Error('not said');
console.log('OK');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_w4_qx_l9_apply_rest_day_keeps_its_label_after_a_refusal():
    _run(_STUBS + _js("applyWeekAutoAdjust") + """
const BTN = {disabled: false, textContent: '🛏 Apply rest day'};
const ST = {textContent: '', innerHTML: ''};
globalThis.window = {_readinessSeverity: 'rest'};
globalThis.document = {getElementById: (id) => id === 'apply-week-tier-down-btn' ? BTN : (id === 'apply-week-tier-down-status' ? ST : null)};
globalThis.fetch = async () => ({ok: false, status: 400, json: async () => ({error: 'Tue 20 Oct is your own ride: click it in the calendar to change or remove it.'})});
(async () => {
  await applyWeekAutoAdjust();
  if (BTN.textContent !== '🛏 Apply rest day') throw new Error('label: ' + BTN.textContent);
  console.log('OK');
})().catch(e => { console.error(e); process.exit(1); });
""")
