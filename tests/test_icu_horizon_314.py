"""3.14.0 F3 — the calendar sync window per rider (2, 4 or 6 weeks), the
clean-up after a shorter window, and the window setting (plan_horizon T1-T13,
grill_integration H1, M1-M4, L1-L3, L6, L7, T14-T26, C7).

Hermetic like tests/test_icu_hold_28.py: stubbed HOME, intervals.icu only
through a fake behind ``icu_calendar_push._http`` that honours the GET
window, the clock frozen at MON. The plan runs 7 weeks from MON with rest on
Mondays and Fridays and one workout file per session, so every workout can
be told apart wherever it sits. Days are written as offsets from MON.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from datetime import timedelta
from pathlib import Path

import pytest

import app as app_module
import icu_calendar_push as icp
import training_planner as tp
from test_icu_push import stub, _sess, _write_plan, _apikey, STEADY_ZWO  # noqa: F401
from test_icu_hold_28 import WindowedFakeICU, MON, _rest, _counts
from test_331_surfaces import _extract_js_function, _run_node

DASHBOARD = Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html"
DEFAULT = icp.HORIZON_DAYS
WIDE = max(icp.HORIZON_CHOICES)
LOOK = icp._LOOKAHEAD_DAYS
N_DAYS = 49
WORK = [i for i in range(N_DAYS) if (MON + timedelta(days=i)).weekday() not in (0, 4)]


def D(i):
    return (MON + timedelta(days=i)).isoformat()


def _id(h, i, n=0, pid=None):
    return f"domestique:{pid or h.pid}:{D(i)}:{n}"


def _plan():
    return [_sess(D(i), zwo=f"w{i:02d}.zwo") if i in WORK else _rest(D(i))
            for i in range(N_DAYS)]


class DocFakeICU(WindowedFakeICU):
    """Bulk-delete as the public API documents it: a row naming an
    external_id deletes every event under it ("if both id and external_id
    are supplied then external_id is used"); a row with only an id deletes
    that one event. (FakeICU deletes by external_id alone.)"""

    def __call__(self, method, path, payload=None, timeout=30.0):
        if method != "PUT" or "PUT" in self.fail:
            return super().__call__(method, path, payload, timeout)
        self.calls.append((method, path, payload))
        ext = {r["external_id"] for r in payload if r.get("external_id")}
        ids = {r["id"] for r in payload if not r.get("external_id")}
        self.events = [e for e in self.events
                       if e.get("external_id") not in ext and e.get("id") not in ids]
        return 200, b"{}"


@pytest.fixture()
def h(stub, freeze_clock, monkeypatch):
    freeze_clock(MON)
    _apikey(stub)
    for i in WORK:
        (stub.workouts / f"w{i:02d}.zwo").write_text(
            STEADY_ZWO.replace("Steady Test", f"W{i:02d}"), encoding="utf-8")
    stub.fk = DocFakeICU()
    monkeypatch.setattr(icp, "_http", stub.fk)
    icp._RECENT_PUSH.clear()
    yield stub
    icp._RECENT_PUSH.clear()


# ── helpers ──────────────────────────────────────────────────────────────────

def _set_window(h, n):
    h.pm.save_prefs({"icu_calendar_horizon_days": n})


def _run(h, **kw):
    r = icp.reconcile(**kw)
    assert not r.get("error") and not r.get("needs_reconnect"), r
    return r


def _seed(h, n=WIDE):
    _set_window(h, n)
    _write_plan(_plan())
    return _run(h)


def _touched(h, since=0):
    """external_ids named in POST / PUT payloads after call index ``since``
    (a delete row by id alone is mapped back to its event's id)."""
    by_id = {e.get("id"): e.get("external_id") for e in h.fk.events}
    posted, deleted = set(), set()
    for method, _path, payload in h.fk.calls[since:]:
        if method == "POST":
            posted |= {ev["external_id"] for ev in payload}
        elif method == "PUT":
            deleted |= {r.get("external_id") or by_id.get(r.get("id"), r.get("id"))
                        for r in payload}
    return posted, deleted


def _gets(h, since=0):
    return [c[1] for c in h.fk.calls[since:] if c[0] == "GET"]


def _state_path(h, pid=None):
    return h.pm._profiles_dir / (pid or h.pid) / "icu_calendar_push.json"


def _state(h, pid=None):
    p = _state_path(h, pid)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _days_of(h, prefix=None):
    """{external_id: [days]} for every event of ours (or of ``prefix``)."""
    prefix = prefix or f"domestique:{h.pid}:"
    out = {}
    for e in h.fk.events:
        if str(e.get("external_id") or "").startswith(prefix):
            out.setdefault(e["external_id"], []).append(e["start_date_local"][:10])
    return {k: sorted(v) for k, v in out.items()}


def _far(lo=DEFAULT, hi=WIDE):
    return [i for i in WORK if lo < i <= hi]


def _send(h, i, zwo=None):
    return h.client.post("/api/calendar/push-workout", json={
        "source": "planner", "zwo_file": zwo or f"w{i:02d}.zwo", "date": D(i)}).json()


def _one_per_session(h):
    """C2 over the whole read of a 6-week window: at most one of our ids per
    workout (a copy the rider made shares the id)."""
    ids = {}
    for e in h.fk.events:
        if str(e.get("external_id") or "").startswith(f"domestique:{h.pid}:"):
            ids.setdefault(Path(e.get("filename") or "").stem, set()).add(e["external_id"])
    dupes = {f: sorted(v) for f, v in ids.items() if len(v) > 1}
    assert not dupes, f"one workout on two days: {dupes}"


def _second_rider(h, sync=False):
    """Profile B: connected (apikey), with a plan, sync off unless asked."""
    pid_b = h.pm.create_profile("Other rider")
    h.pm.switch(pid_b)
    h.pm.save_env("i456", "KEY456")
    _write_plan(_plan())
    if sync:
        h.pm.save_prefs({"icu_calendar_sync": True})
    h.pm.switch(h.pid)
    return pid_b


def _spy_lock_wait(monkeypatch, name):
    """Event set when a call of ``name`` reaches the reconcile lock."""
    arrived, real = threading.Event(), icp._one_at_a_time

    def spy(timeout_s, fn, *args):
        if fn.__name__ == name:
            arrived.set()
        return real(timeout_s, fn, *args)

    monkeypatch.setattr(icp, "_one_at_a_time", spy)
    return arrived


# ── T1, T2: the window follows the setting; a shorter one cleans up ─────────

def test_the_window_follows_the_riders_setting(h):
    r = _seed(h, WIDE)
    assert _gets(h) == [f"athlete/i123/events?oldest={D(-7)}&newest={D(WIDE + LOOK)}"]
    posted, _ = _touched(h)
    assert posted == {_id(h, i) for i in WORK if i <= WIDE}
    assert r["pushed"] == len(posted)
    assert _state(h)["pushed_until"] == D(WIDE)
    _one_per_session(h)


def test_a_shorter_window_removes_what_the_longer_one_put_further_out(h):
    _seed(h, WIDE)
    _set_window(h, DEFAULT)
    mark = len(h.fk.calls)
    r = _run(h)
    assert _gets(h, mark) == [f"athlete/i123/events?oldest={D(-7)}&newest={D(WIDE + LOOK)}"]
    posted, deleted = _touched(h, mark)
    assert deleted == {_id(h, i) for i in _far()} and r["deleted"] == len(_far())
    assert posted == {_id(h, i) for i in WORK if i <= DEFAULT}
    assert _days_of(h) == {_id(h, i): [D(i)] for i in WORK if i <= DEFAULT}
    assert _state(h)["pushed_until"] is None            # back to the default: no reach
    mark = len(h.fk.calls)
    assert _counts(_run(h)) == (0, 0, 0)
    assert _gets(h, mark) == [f"athlete/i123/events?oldest={D(-7)}&newest={D(DEFAULT + LOOK)}"]


def test_the_clean_up_spares_moves_copies_foreign_manual_far_sends_and_the_past(h):
    """T3: cross-week drags, so #33 has nothing to learn."""
    _seed(h, WIDE)
    h.fk.drag(_id(h, 30), D(36))                        # Wed wk5 → Tue wk6: held
    h.fk.copy(_id(h, 29), D(31))                        # a copy shares the id: held
    foreign = f"domestique:other-profile:{D(35)}:0"
    manual = f"domestique-manual:{h.pid}:abcd1234"
    h.fk.events += [
        {"id": 1, "external_id": foreign, "start_date_local": f"{D(35)}T00:00:00",
         "name": "other install", "filename": "other.zwo", "category": "WORKOUT"},
        {"id": 2, "external_id": manual, "start_date_local": f"{D(36)}T00:00:00",
         "name": "library push", "filename": "lib.zwo", "category": "WORKOUT"},
        {"id": 3, "external_id": _id(h, -2), "start_date_local": f"{D(-2)}T00:00:00",
         "name": "ridden", "filename": "past.zwo", "category": "WORKOUT"},
    ]
    assert _send(h, 45)["pushed"] == 1                  # past the 6-week window
    _set_window(h, DEFAULT)
    mark = len(h.fk.calls)
    r = _run(h)
    _posted, deleted = _touched(h, mark)
    kept = {_id(h, 30): [D(36)], _id(h, 29): [D(29), D(31)], _id(h, 45): [D(45)],
            _id(h, -2): [D(-2)]}
    for eid, days in kept.items():
        assert _days_of(h)[eid] == days, eid
    assert {foreign, manual} <= {e["external_id"] for e in h.fk.events}
    assert deleted == {_id(h, i) for i in _far()} - {_id(h, 29), _id(h, 30)}
    assert r["deleted"] == len(deleted)
    # The record still reaches the moved and copied events it left out there.
    assert _state(h)["pushed_until"] == D(36)


# ── M4 / O5: moves the plan took over, and far moves learned ────────────────

def test_a_far_move_inside_the_window_is_learned(h):
    """O5: with 6 weeks, a plain move in week 5 is learned like any other."""
    _seed(h, WIDE)
    icp._RECENT_PUSH.clear()                            # the rider drags later
    h.fk.drag(_id(h, 31), D(32))                        # Thu wk5 → the Fri rest day
    r = _run(h)
    assert [(m["from"], m["to"]) for m in r["learned"]] == [(D(31), D(32))]
    plan = json.loads((Path(tp.PLAN_DIR) / "current_plan.json").read_text())
    fri = next(s for w in plan["weeks"] for s in w["sessions"] if s["day"] == D(32))
    assert (fri["zwo_file"], fri["user_moved"], fri["moved_from"]) == ("w31.zwo", True, D(31))
    assert _days_of(h)[_id(h, 32)] == [D(32)] and _id(h, 31) not in _days_of(h)


def test_a_learned_far_move_survives_a_shorter_window_and_sync_off_removes_it(h):
    """M4 (T19, learning on): the plan followed the rider's move, so the
    clean-up leaves that workout where they put it; the record keeps
    reaching it, so turning sync off still removes it."""
    _seed(h, WIDE)
    icp._RECENT_PUSH.clear()
    h.fk.drag(_id(h, 31), D(32))
    assert len(_run(h)["learned"]) == 1
    _set_window(h, DEFAULT)
    mark = len(h.fk.calls)
    _run(h)
    _posted, deleted = _touched(h, mark)
    assert _days_of(h)[_id(h, 32)] == [D(32)]
    assert deleted == {_id(h, i) for i in _far() if i != 31}
    assert _state(h)["pushed_until"] == D(32)
    r = icp.sweep_all()
    assert not r.get("error") and _days_of(h) == {}
    assert _state(h)["pushed_until"] is None


# ── T4-T6: failures, sync off ───────────────────────────────────────────────

def test_a_failed_clean_up_is_retried(h):
    _seed(h, WIDE)
    _set_window(h, DEFAULT)
    h.fk.fail["PUT"] = (500, b"x")
    r = icp.reconcile()
    assert r["error"] == "sweep_failed"
    assert all(_id(h, i) in _days_of(h) for i in _far())
    assert _state(h)["pushed_until"] == D(WIDE)          # kept for the retry
    del h.fk.fail["PUT"]
    r = _run(h)
    assert r["deleted"] == len(_far()) and _state(h)["pushed_until"] is None


def test_sync_off_after_six_weeks_removes_everything_ours(h):
    _seed(h, WIDE)
    manual = f"domestique-manual:{h.pid}:abcd1234"
    h.fk.events += [
        {"id": 2, "external_id": manual, "start_date_local": f"{D(20)}T00:00:00",
         "name": "library push", "filename": "lib.zwo", "category": "WORKOUT"},
        {"id": 3, "external_id": _id(h, -2), "start_date_local": f"{D(-2)}T00:00:00",
         "name": "ridden", "filename": "past.zwo", "category": "WORKOUT"},
    ]
    mark = len(h.fk.calls)
    r = icp.sweep_all()
    assert _gets(h, mark) == [f"athlete/i123/events?oldest={D(0)}&newest={D(WIDE + LOOK)}"]
    assert r["deleted"] == len([i for i in WORK if i <= WIDE])
    assert _days_of(h) == {_id(h, -2): [D(-2)]}
    assert manual in {e["external_id"] for e in h.fk.events}
    assert _state(h)["pushed_until"] is None


def test_sync_off_after_a_failed_shrink_reaches_the_old_window(h):
    _seed(h, WIDE)
    _set_window(h, DEFAULT)
    h.fk.fail["PUT"] = (500, b"x")
    assert icp.reconcile()["error"] == "sweep_failed"
    del h.fk.fail["PUT"]
    mark = len(h.fk.calls)
    r = icp.sweep_all()
    assert _gets(h, mark) == [f"athlete/i123/events?oldest={D(0)}&newest={D(WIDE + LOOK)}"]
    assert not r.get("error") and _days_of(h) == {}


# ── T7: one rider's window never reaches another rider ──────────────────────

def test_one_riders_window_never_reaches_another_rider(h):
    pid_b = _second_rider(h)
    _seed(h, WIDE)                                      # A: 6 weeks
    h.pm.switch(pid_b)
    mark = len(h.fk.calls)
    _run(h)
    posted, _ = _touched(h, mark)
    assert posted == {_id(h, i, pid=pid_b) for i in WORK if i <= DEFAULT}
    assert _state(h, pid_b) is None
    h.pm.switch(h.pid)
    mark = len(h.fk.calls)
    _run(h)
    posted, _ = _touched(h, mark)
    assert posted == {_id(h, i) for i in WORK if i <= WIDE}


def test_a_profile_switch_during_the_read_keeps_the_run_on_its_rider(h, monkeypatch):
    pid_b = _second_rider(h)
    _set_window(h, WIDE)
    _write_plan(_plan())
    fk, state = h.fk, {"switched": False}

    def http(method, path, payload=None, timeout=30.0):
        if method == "GET" and not state["switched"]:
            state["switched"] = True
            h.pm.switch(pid_b)
        return fk(method, path, payload, timeout)

    monkeypatch.setattr(icp, "_http", http)
    icp.reconcile()
    posted, _ = _touched(h)
    assert posted == {_id(h, i) for i in WORK if i <= WIDE}
    assert _state(h)["pushed_until"] == D(WIDE)         # in A's folder
    assert _state(h, pid_b) is None


# ── T8, T23: the Send button reads as far as the reconcile ──────────────────

def test_the_send_button_reads_the_riders_window(h):
    _seed(h, WIDE)
    h.fk.drag(_id(h, 20), D(29))                        # Sun wk3 → Tue wk5
    mark = len(h.fk.calls)
    assert _send(h, 20) == {"ok": True, "pushed": 0, "kept_on": D(29)}
    assert not [c for c in h.fk.calls[mark:] if c[0] == "POST"]


def test_the_send_button_reads_as_far_as_a_pending_clean_up(h):
    """L3: window shrunk with sync off (saved only, clean-up pending)."""
    _seed(h, WIDE)
    _set_window(h, DEFAULT)
    h.fk.drag(_id(h, 20), D(30))                        # Sun wk3 → Wed wk5
    mark = len(h.fk.calls)
    assert _send(h, 20) == {"ok": True, "pushed": 0, "kept_on": D(30)}
    assert not [c for c in h.fk.calls[mark:] if c[0] == "POST"]


# ── T14, T15 (H1): a Send after the shrink survives its clean-up ────────────

def test_a_send_after_shrinking_with_sync_off_survives_the_clean_up(h):
    _seed(h, WIDE)
    r = h.client.post("/api/icu/push", json={"horizon_days": DEFAULT}).json()
    assert r["horizon_days"] == DEFAULT and r["sync_enabled"] is False
    assert _send(h, 30)["pushed"] == 1
    r = h.client.post("/api/icu/push", json={}).json()          # the Push button
    assert r["ok"], r
    assert _days_of(h)[_id(h, 30)] == [D(30)]
    assert r["deleted"] == len(_far()) - 1
    assert all(_id(h, i) not in _days_of(h) for i in _far() if i != 30)


def test_a_send_after_a_failed_clean_up_survives_the_retry(h):
    h.pm.save_prefs({"icu_calendar_sync": True})
    _write_plan(_plan())
    assert h.client.post("/api/icu/push", json={"horizon_days": WIDE}).json()["ok"]
    h.fk.fail["PUT"] = (500, b"x")
    r = h.client.post("/api/icu/push", json={"horizon_days": DEFAULT}).json()
    assert r["horizon_days"] == DEFAULT and r["error"] == "sweep_failed"
    del h.fk.fail["PUT"]
    assert _send(h, 30)["pushed"] == 1
    r = h.client.post("/api/icu/push", json={}).json()
    assert r["ok"] and r["deleted"] == len(_far()) - 1
    assert _days_of(h)[_id(h, 30)] == [D(30)]


# ── T9, T10, T24: the setting and its endpoint ──────────────────────────────

@pytest.mark.parametrize("bad", [21, "28", True, None, 0, 1000, 28.0])
def test_a_bad_window_is_refused_without_a_call(h, bad):
    _set_window(h, 28)
    r = h.client.post("/api/icu/push", json={"horizon_days": bad}).json()
    assert r == {"ok": False, "error": "bad_horizon"}
    prefs = json.loads((h.pm._profiles_dir / h.pid / "user_prefs.json").read_text())
    assert prefs["icu_calendar_horizon_days"] == 28
    assert h.fk.calls == []


def test_the_setting_is_saved_per_profile(h):
    r = h.client.post("/api/icu/push", json={"horizon_days": 28}).json()
    assert (r["ok"], r["horizon_days"], r["sync_enabled"]) == (True, 28, False)
    prefs = json.loads((h.pm._profiles_dir / h.pid / "user_prefs.json").read_text())
    assert prefs["icu_calendar_horizon_days"] == 28
    assert h.client.get("/api/icu/push").json()["horizon_days"] == 28
    assert h.fk.calls == []                             # sync off: saved only
    pid_b = _second_rider(h)
    h.pm.switch(pid_b)
    assert h.client.get("/api/icu/push").json()["horizon_days"] == DEFAULT


def test_the_status_without_an_active_profile_reads_the_default(h, monkeypatch):
    monkeypatch.setattr(h.pm, "_active_id", None)
    s = h.client.get("/api/icu/push")
    assert s.status_code == 200 and s.json()["horizon_days"] == DEFAULT
    r = h.client.post("/api/icu/push", json={"horizon_days": 28}).json()
    assert r == {"ok": False, "error": "no_active_profile"}


def test_changing_the_window_with_sync_on_updates_the_calendar_now(h):
    h.pm.save_prefs({"icu_calendar_sync": True})
    _write_plan(_plan())
    _run(h)
    r = h.client.post("/api/icu/push", json={"horizon_days": WIDE}).json()
    assert (r["ok"], r["horizon_days"], r["pushed"]) == (True, WIDE, len(_far()))
    assert set(_days_of(h)) == {_id(h, i) for i in WORK if i <= WIDE}
    r = h.client.post("/api/icu/push", json={"horizon_days": DEFAULT}).json()
    assert (r["ok"], r["deleted"]) == (True, len(_far()))
    s = h.client.get("/api/icu/push").json()
    assert s["horizon_days"] == DEFAULT and s["last_result"]["deleted"] == len(_far())


# ── T16, T17 (M2): the immediate update is bound to its rider and to sync ───

def test_sync_turned_off_while_the_window_change_waits_pushes_nothing(h, monkeypatch):
    h.pm.save_prefs({"icu_calendar_sync": True})
    _write_plan(_plan())
    monkeypatch.setattr(app_module, "_icu_push_last_result", None)
    arrived = _spy_lock_wait(monkeypatch, "_reconcile_unless_swept")
    out = {}
    assert icp._RECONCILE_LOCK.acquire(timeout=1)
    try:
        t = threading.Thread(target=lambda: out.setdefault(
            "r", app_module.api_icu_push({"horizon_days": WIDE})))
        t.start()
        assert arrived.wait(5)
        h.pm.save_prefs({"icu_calendar_sync": False})   # e.g. another window
    finally:
        icp._RECONCILE_LOCK.release()
    t.join(10)
    assert out["r"]["horizon_days"] == WIDE and out["r"]["error"] == "sync_off"
    assert h.fk.calls == [] and app_module._icu_push_last_result is None


def test_a_sweep_while_the_window_change_waits_is_not_undone(h, monkeypatch):
    h.pm.save_prefs({"icu_calendar_sync": True})
    _write_plan(_plan())
    _run(h)
    assert _days_of(h)
    real = icp._one_at_a_time
    arrived, go, out = threading.Event(), threading.Event(), {}

    def gated(timeout_s, fn, *args):
        if fn.__name__ == "_reconcile_unless_swept":
            arrived.set()
            go.wait(10)                                 # the sweep goes first
        return real(timeout_s, fn, *args)

    monkeypatch.setattr(icp, "_one_at_a_time", gated)
    t = threading.Thread(target=lambda: out.setdefault(
        "r", app_module.api_icu_push({"horizon_days": WIDE})))
    t.start()
    assert arrived.wait(5)
    assert app_module.api_icu_push({"sync": False})["sync_enabled"] is False
    assert _days_of(h) == {}
    go.set()
    t.join(10)
    assert out["r"]["error"] in ("swept", "sync_off") and _days_of(h) == {}


def test_a_profile_switch_while_the_window_change_waits_runs_nothing(h, monkeypatch):
    pid_b = _second_rider(h)                            # B: connected, sync off
    h.pm.save_prefs({"icu_calendar_sync": True})
    _write_plan(_plan())
    arrived = _spy_lock_wait(monkeypatch, "_reconcile_unless_swept")
    out = {}
    assert icp._RECONCILE_LOCK.acquire(timeout=1)
    try:
        t = threading.Thread(target=lambda: out.setdefault(
            "r", app_module.api_icu_push({"horizon_days": WIDE})))
        t.start()
        assert arrived.wait(5)
        h.pm.switch(pid_b)
    finally:
        icp._RECONCILE_LOCK.release()
    t.join(10)
    assert out["r"]["error"] == "profile_changed"
    assert h.fk.calls == [] and _state(h, pid_b) is None
    a_prefs = json.loads((h.pm._profiles_dir / h.pid / "user_prefs.json").read_text())
    assert a_prefs["icu_calendar_horizon_days"] == WIDE


def test_a_debounced_push_waiting_for_the_lock_never_runs_for_the_next_rider(h, monkeypatch):
    """Grill M2, the same race before 3.14.0: the plan-write push checked
    its rider before waiting for the lock, then ran for whoever was active.
    B has sync on here, so only the binding to A stops it."""
    pid_b = _second_rider(h, sync=True)
    h.pm.save_prefs({"icu_calendar_sync": True})
    _write_plan(_plan())
    monkeypatch.setattr(app_module, "_icu_push_last_result", None)
    arrived = _spy_lock_wait(monkeypatch, "_reconcile_unless_swept")
    assert icp._RECONCILE_LOCK.acquire(timeout=1)
    try:
        t = threading.Thread(target=app_module._icu_push_debounced_run, args=(h.pid,))
        t.start()
        assert arrived.wait(5)
        h.pm.switch(pid_b)
    finally:
        icp._RECONCILE_LOCK.release()
    t.join(10)
    assert h.fk.calls == [] and app_module._icu_push_last_result is None


def test_a_disconnect_waiting_for_the_lock_never_touches_the_next_rider(h, monkeypatch):
    """Grill M2 (pre-existing): the disconnect's sweep waited up to two
    minutes, then swept and cleared whichever rider was active by then."""
    pid_b = _second_rider(h)
    h.pm.switch(pid_b)
    h.pm.save_icu_token("TOKB", "i456", granted_scopes="CALENDAR:WRITE")
    _run(h)                                             # B's workouts are out
    b_events = _days_of(h, f"domestique:{pid_b}:")
    assert b_events
    h.pm.switch(h.pid)
    arrived = _spy_lock_wait(monkeypatch, "_sweep_then_bump")
    out = {}
    assert icp._RECONCILE_LOCK.acquire(timeout=1)
    try:
        t = threading.Thread(target=lambda: out.setdefault("r", app_module.api_icu_disconnect()))
        t.start()
        assert arrived.wait(5)
        h.pm.switch(pid_b)
    finally:
        icp._RECONCILE_LOCK.release()
    t.join(10)
    assert out["r"].status_code == 409
    assert json.loads(out["r"].body)["error"] == "profile_changed"
    assert h.pm.active_id == pid_b and h.pm.icu_access_token == "TOKB"
    assert _days_of(h, f"domestique:{pid_b}:") == b_events
    assert not [c for c in h.fk.calls if c[0] == "PUT"]


# ── T11, T18, T21, T22, T25: the record fails closed ───────────────────────

def test_a_garbled_record_never_widens_the_sweep(h):
    _write_plan(_plan())
    _run(h)
    assert _send(h, 20)["pushed"] == 1                  # past the default window
    _state_path(h).write_text("{not json", encoding="utf-8")
    r = _run(h)
    assert r["deleted"] == 0 and _days_of(h)[_id(h, 20)] == [D(20)]


def test_a_far_record_reaches_no_further_than_six_weeks(h):
    _write_plan(_plan())
    _state_path(h).write_text(json.dumps(
        {"pushed_until": D(200), "written_on": D(0), "sent": {}}), encoding="utf-8")
    assert _send(h, 45)["pushed"] == 1
    mark = len(h.fk.calls)
    _run(h)
    assert _gets(h, mark) == [f"athlete/i123/events?oldest={D(-7)}&newest={D(WIDE + LOOK)}"]
    assert _days_of(h)[_id(h, 45)] == [D(45)]


def test_a_default_rider_reads_and_sweeps_exactly_as_before(h, freeze_clock):
    """P1 + T18 (M1): never a record for a rider who never chose a window,
    so nothing reaches past the window even when the date goes back."""
    _write_plan(_plan())
    for day in (1, 0):                                  # forward, then back
        freeze_clock(MON + timedelta(days=day))
        mark = len(h.fk.calls)
        _run(h)
        assert _gets(h, mark) == [
            f"athlete/i123/events?oldest={D(day - 7)}&newest={D(day + DEFAULT + LOOK)}"]
        assert _state(h) is None
    assert _send(h, DEFAULT + 1)["pushed"] == 1         # one day past the window
    r = _run(h)
    assert r["deleted"] == 0 and _days_of(h)[_id(h, DEFAULT + 1)] == [D(DEFAULT + 1)]
    mark = len(h.fk.calls)
    icp.sweep_all()
    assert _gets(h, mark) == [f"athlete/i123/events?oldest={D(0)}&newest={D(DEFAULT + LOOK)}"]
    assert _state(h) is None


def test_a_record_written_on_a_later_day_reaches_nothing(h, freeze_clock):
    """M1: the clock went back after a 6-week run."""
    freeze_clock(MON + timedelta(days=3))
    _seed(h, WIDE)
    assert _state(h)["written_on"] == D(3)
    freeze_clock(MON)
    _set_window(h, DEFAULT)
    r = _run(h)
    assert r["deleted"] == 0
    assert all(_id(h, i) in _days_of(h) for i in _far())


def test_unreadable_prefs_never_clean_up(h, monkeypatch):
    """L1: a prefs file briefly locked (e.g. by a virus scanner)."""
    _seed(h, WIDE)
    prefs = h.pm._profiles_dir / h.pid / "user_prefs.json"
    real = Path.read_text

    def locked(self, *a, **k):
        if self == prefs:
            raise PermissionError("locked")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", locked)
    mark = len(h.fk.calls)
    r = _run(h)
    assert r["deleted"] == 0 and not [c for c in h.fk.calls[mark:] if c[0] == "PUT"]
    posted, _ = _touched(h, mark)
    assert posted == {_id(h, i) for i in WORK if i <= DEFAULT}
    assert _state(h)["pushed_until"] == D(WIDE)


def test_an_explicit_window_never_moves_the_record(h):
    """L2: only a run that reads the rider's setting cleans up."""
    _seed(h, WIDE)
    mark = len(h.fk.calls)
    r = _run(h, horizon_days=DEFAULT)
    assert r["deleted"] == 0
    assert _gets(h, mark) == [f"athlete/i123/events?oldest={D(-7)}&newest={D(DEFAULT + LOOK)}"]
    assert _state(h)["pushed_until"] == D(WIDE)
    assert all(_id(h, i) in _days_of(h) for i in _far())


def test_a_failed_record_write_never_blocks_the_push(h, monkeypatch, caplog):
    """R5 / T25."""
    _set_window(h, WIDE)
    _write_plan(_plan())
    real = Path.write_text

    def read_only(self, *a, **k):
        if self.name == "icu_calendar_push.json.tmp":
            raise PermissionError("read-only")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", read_only)
    with caplog.at_level(logging.WARNING, logger="domestique.icu_push"):
        r = _run(h)
    assert r["pushed"] == len([i for i in WORK if i <= WIDE])
    assert any("EVENT=icu_calendar_state_write_failed" in m for m in caplog.messages)
    assert _state(h) is None


# ── L6: the held count only counts what the window itself reads ─────────────

def test_the_held_count_does_not_change_without_the_rider(h):
    _seed(h, WIDE)
    h.fk.drag(_id(h, 41), D(48))                        # Sun wk6 → Sun wk7
    assert _run(h)["held"] == 1
    _set_window(h, DEFAULT)
    first, second = _run(h), _run(h)                     # the clean-up, then the next
    assert first["held"] == second["held"] == 0
    assert _days_of(h)[_id(h, 41)] == [D(48)]


# ── C7 (grill M3): delete rows by event id, behind the constant ─────────────

@pytest.mark.parametrize("by_id", [False, True])
def test_a_copy_beyond_the_read_and_the_delete_rows(h, monkeypatch, by_id):
    monkeypatch.setattr(icp, "_DELETE_BY_ID_ONLY", by_id)
    _seed(h, WIDE)
    copy_id = 9000
    h.fk.events.append(dict(next(e for e in h.fk.events if e["external_id"] == _id(h, 36)),
                            id=copy_id, start_date_local=f"{D(52)}T00:00:00"))
    _set_window(h, DEFAULT)
    mark = len(h.fk.calls)
    _run(h)
    rows = next(c[2] for c in h.fk.calls[mark:] if c[0] == "PUT")
    if by_id:
        assert all(set(r) == {"id"} for r in rows)
        assert any(e.get("id") == copy_id for e in h.fk.events)       # the copy stays
    else:
        assert all(set(r) == {"id", "external_id"} for r in rows)     # today's payload
        assert not any(e.get("id") == copy_id for e in h.fk.events)   # documented: it goes
    assert not [e for e in h.fk.events if e["external_id"] == _id(h, 36)
                and e["start_date_local"][:10] == D(36)]


@pytest.mark.parametrize("by_id", [False, True])
def test_sync_off_delete_rows_follow_the_setting(h, monkeypatch, by_id):
    monkeypatch.setattr(icp, "_DELETE_BY_ID_ONLY", by_id)
    _seed(h, WIDE)
    h.fk.copy(_id(h, 20), D(22))                        # a copy inside the read
    copy_id = 9001
    h.fk.events.append(dict(next(e for e in h.fk.events if e["external_id"] == _id(h, 36)),
                            id=copy_id, start_date_local=f"{D(52)}T00:00:00"))
    r = icp.sweep_all()
    assert not r.get("error")
    left = [e for e in h.fk.events if str(e.get("external_id")).startswith(f"domestique:{h.pid}:")]
    assert [e["id"] for e in left] == ([copy_id] if by_id else [])


# ── T12, T13, T26: the Settings row ─────────────────────────────────────────

def _sync_block(src):
    start = src.index('id="icu-cal-sync-block"')
    return src[start:src.index("</div>\n    </div>", start)]


def test_the_window_row_offers_the_three_choices_with_a_label():
    src = DASHBOARD.read_text(encoding="utf-8")
    block = _sync_block(src)
    options = re.findall(r'<option value="(\d+)">([^<]+)</option>', block)
    assert [(int(v), t) for v, t in options] == [
        (n, f"{n // 7} weeks") for n in icp.HORIZON_CHOICES]
    assert 'for="icu-cal-window"' in block and 'id="icu-cal-window"' in block
    assert re.search(r'id="icu-cal-window-status"[^>]*role="status"', block)


def test_no_fourteen_days_left_in_the_calendar_sync_text():
    src = DASHBOARD.read_text(encoding="utf-8")
    text = _sync_block(src) + "".join(
        _extract_js_function(src, fn) for fn in (
            "icuPushRefreshButton", "icuCalSyncRefresh", "icuCalSyncToggle",
            "icuCalWindowNote", "icuCalWindowSet"))
    assert "14 days" not in text and "14-day" not in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_window_select_reflects_and_sets_the_setting():
    src = DASHBOARD.read_text(encoding="utf-8")
    harness = "".join(_extract_js_function(src, fn) + "\n" for fn in (
        "icuCalSyncRefresh", "icuCalWindowNote", "icuCalWindowSet")) + """
const els = {};
global.$ = (id) => els[id] || (els[id] = {id, style: {}, value: '', textContent: '',
                                          innerHTML: '', checked: false, disabled: false});
let answer, posts = [], toasts = [];
global.fetch = async (url, opts) => {
  if (opts && opts.method === 'POST') posts.push(JSON.parse(opts.body));
  return {json: async () => answer};
};
global.icuPushToastFromResult = (r, verb) => toasts.push(verb + ':' + r.pushed);
global.icuPushRefreshButton = () => {};
(async () => {
  answer = {connected: true, write_ok: true, sync_enabled: true, horizon_days: 28};
  await icuCalSyncRefresh();
  if ($('icu-cal-window').value !== '28') throw new Error('select ' + $('icu-cal-window').value);
  const sel = $('icu-cal-window');
  sel.value = '42';
  answer = {ok: true, horizon_days: 42, sync_enabled: true, pushed: 18, updated: 0, deleted: 0, skipped: []};
  await icuCalWindowSet(sel);
  if (JSON.stringify(posts) !== JSON.stringify([{horizon_days: 42}])) throw new Error('posts ' + JSON.stringify(posts));
  if ($('icu-cal-window-status').textContent !== 'Saved.') throw new Error('status ' + $('icu-cal-window-status').textContent);
  if (JSON.stringify(toasts) !== JSON.stringify(['Pushed to intervals.icu:18'])) throw new Error('toasts ' + JSON.stringify(toasts));
  answer = {ok: true, horizon_days: 42, sync_enabled: false, pushed: 0};
  await icuCalWindowSet(sel);
  if ($('icu-cal-window-status').textContent !== 'Saved. Push to intervals.icu calendar now sends 6 weeks.')
    throw new Error('sync off ' + $('icu-cal-window-status').textContent);
  answer = {ok: false, error: 'bad_horizon'};
  await icuCalWindowSet(sel);
  if ($('icu-cal-window-status').style.color !== 'var(--red)') throw new Error('not red');
  if (toasts.length !== 1) throw new Error('toast on a failure');
  console.log('OK');
})().catch((e) => { console.error(e); process.exit(1); });
"""
    _run_node(harness)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_push_button_and_the_sync_toast_name_the_riders_weeks():
    src = DASHBOARD.read_text(encoding="utf-8")
    harness = "".join(_extract_js_function(src, fn) + "\n" for fn in (
        "icuPushRefreshButton", "icuCalSyncToggle")) + """
const els = {};
global.$ = (id) => els[id] || (els[id] = {id, style: {}, value: '42', textContent: '',
                                          disabled: false, checked: true});
let answer = {connected: true, write_ok: true, horizon_days: 28};
global.fetch = async () => ({json: async () => answer});
const toasts = [];
global.showToast = (m) => toasts.push(m);
global.icuPushToastFromResult = () => {};
global.icuCalSyncRefresh = () => {};
global.icuPushNow = () => {};
global.openSettingsConnections = () => {};
(async () => {
  await icuPushRefreshButton();
  if (!$('btn-icu-push').title.startsWith('Send the next 4 weeks of your plan'))
    throw new Error($('btn-icu-push').title);
  answer = {ok: true, sync_enabled: true};
  await icuCalSyncToggle({checked: true, disabled: false});
  if (toasts[0] !== 'Calendar sync on — plan changes now mirror to intervals.icu (next 6 weeks).')
    throw new Error(toasts[0]);
  console.log('OK');
})().catch((e) => { console.error(e); process.exit(1); });
"""
    _run_node(harness)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_each_answer_gets_its_own_words():
    """L7 / T26: the window row and the shared toast."""
    src = DASHBOARD.read_text(encoding="utf-8")
    harness = (_extract_js_function(src, "icuCalWindowNote") + "\n"
               + _extract_js_function(src, "icuPushToastFromResult")) + """
const base = {horizon_days: 28, sync_enabled: true, pushed: 0, updated: 0, deleted: 0, skipped: []};
const note = (extra) => icuCalWindowNote(Object.assign({}, base, extra));
const want = [
  [note({}), 'Saved.'],
  [note({error: 'swept'}), 'Saved.'],
  [note({error: 'sync_off'}), 'Saved.'],
  [note({error: 'profile_changed'}), 'Saved.'],
  [note({error: 'no_plan'}), 'Saved. There is no plan to send yet.'],
  [note({error: 'busy'}), 'Saved. Another calendar update was running; your calendar catches up at its next update.'],
  [note({error: 'network'}), 'Saved. intervals.icu could not be reached just now; your calendar catches up at its next update.'],
  [note({needs_reconnect: true}), 'Saved. intervals.icu needs a quick reconnect to allow calendar writes (button above).'],
  [note({sync_enabled: false}), 'Saved. Push to intervals.icu calendar now sends 4 weeks.'],
  [icuCalWindowNote({ok: false, error: 'no_active_profile'}), 'Choose a profile first.'],
  [icuCalWindowNote({ok: false, error: 'bad_horizon'}), 'Could not save — try again.'],
  [icuCalWindowNote(null), 'Could not save — try again.'],
];
for (const [got, exp] of want) if (got !== exp) throw new Error(JSON.stringify([got, exp]));
const seen = [];
global.showToast = (m) => seen.push(m);
icuPushToastFromResult({pushed: 0, skipped: [], error: 'no_plan'}, 'Pushed to intervals.icu');
icuPushToastFromResult({pushed: 0, skipped: [], error: 'profile_changed'}, 'Calendar sync off — cleanup');
const toasts = ['There is no plan to send to intervals.icu yet.',
                'Another profile is active now — nothing was changed in intervals.icu.'];
if (JSON.stringify(seen) !== JSON.stringify(toasts)) throw new Error(JSON.stringify(seen));
console.log('OK');
"""
    _run_node(harness)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_held_line_and_the_toast_name_the_riders_rides_as_rides():
    """QA-INT S2 (Wave 4): the Settings line no longer calls the rider's
    rides "workouts", nor says Domestique shows a held workout on its planned
    day (after an own ride took that day, it does not)."""
    src = DASHBOARD.read_text(encoding="utf-8")
    harness = (_extract_js_function(src, "icuHeldLine") + "\n"
               + _extract_js_function(src, "icuPushToastFromResult")) + """
const want = [
  [icuHeldLine(0, 0), ''],
  [icuHeldLine(1, 0), 'Last update: 1 workout kept where you moved it in intervals.icu.'],
  [icuHeldLine(1, 1), 'Last update: 1 of your rides is on another day in intervals.icu: open it in the calendar to follow it or put it back.'],
  [icuHeldLine(4, 2), 'Last update: 2 workouts kept where you moved them in intervals.icu. 2 of your rides are on another day in intervals.icu: open them in the calendar to follow them or put them back.'],
];
for (const [got, exp] of want) if (got !== exp) throw new Error(JSON.stringify([got, exp]));
const seen = [];
global.showToast = (m) => seen.push(m);
icuPushToastFromResult({pushed: 0, updated: 0, deleted: 0, skipped: [], held: 3, held_rides: 2}, 'Pushed to intervals.icu');
if (seen[0] !== 'Pushed to intervals.icu: calendar already up to date · 1 workout kept where you moved it in intervals.icu · 2 of your rides are on another day in intervals.icu')
  throw new Error(seen[0]);
console.log('OK');
"""
    _run_node(harness)
