"""ICU calendar push — IP_ICU_PUSH (v3.0.1) contract tests.

Hermetic: every test runs against a stubbed HOME (fresh ProfileManager under
tmp_path) and the ICU transport is mocked at the engine's ONE seam
(``icu_calendar_push._http``). The conftest network block stays king — no
test here can reach the live API even if a mock is missing.

Contract coverage:
  G1   idempotency — second reconcile reports 0 changes
  G2   net calendar state after dismiss / rematch / move; double-threshold
       day yields TWO events (:0 and :1)
  G3   hr target_mode pushes a FIT whose steps carry HEART_RATE targets
       (C8 semantics: short reps OPEN/RPE by design); power mode pushes the
       matched ZWO byte-identical
  G4   missing write scope → clean needs_reconnect, zero exceptions
       (both the unstamped-OAuth pre-check and the 403-scope-body path)
  G5   mixed-calendar sweep deletes ONLY our profile's events; foreign
       "domestique:" prefixes are warned about, never touched (G-B)
  G-E  a FIT ending in an OPEN step is skipped (ICU drops trailing OPEN)
  G-H  the window starts today — past-day events are never touched
  G-A  toggle OFF runs the final forward sweep; disconnect sweeps BEFORE
       the token purge
  G-F  /api/icu/connection write_ok: apikey always, OAuth only with the
       CALENDAR:WRITE stamp
"""
from __future__ import annotations

import base64
import json
import os
import sys
import types
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import db as db_module  # noqa: E402
import app as app_module  # noqa: E402
import training_planner as tp  # noqa: E402
import icu_calendar_push as icp  # noqa: E402
from profile_manager import ProfileManager  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
_ENV_KEYS = ("ICU_ATHLETE_ID", "ICU_API_KEY", "ICU_ACCESS_TOKEN")

TODAY = date.today()
D1 = (TODAY + timedelta(days=1)).isoformat()
D2 = (TODAY + timedelta(days=2)).isoformat()
D3 = (TODAY + timedelta(days=3)).isoformat()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()

STEADY_ZWO = """<workout_file>
    <name>Steady Test</name>
    <workout>
        <Warmup Duration="600" PowerLow="0.5" PowerHigh="0.7"/>
        <SteadyState Duration="1200" Power="0.7"/>
        <Cooldown Duration="300" PowerLow="0.7" PowerHigh="0.5"/>
    </workout>
</workout_file>
"""

TEMPO_ZWO = """<workout_file>
    <name>Tempo Alt</name>
    <workout>
        <Warmup Duration="600" PowerLow="0.5" PowerHigh="0.75"/>
        <SteadyState Duration="1800" Power="0.85"/>
        <Cooldown Duration="300" PowerLow="0.7" PowerHigh="0.5"/>
    </workout>
</workout_file>
"""

# Ends with a 30 s @ 200%FTP sprint — under HR_MIN_SEG_S the hr converter
# makes that final step OPEN/RPE → the G-E trailing-OPEN guard must skip it.
SPRINT_END_ZWO = """<workout_file>
    <name>Sprint End</name>
    <workout>
        <Warmup Duration="600" PowerLow="0.5" PowerHigh="0.7"/>
        <SteadyState Duration="300" Power="0.9"/>
        <SteadyState Duration="30" Power="2.0"/>
    </workout>
</workout_file>
"""


# ---------------------------------------------------------------------------
# Stubbed-HOME fixture (pattern: tests/test_account_flow.py)
# ---------------------------------------------------------------------------

@pytest.fixture()
def stub(tmp_path):
    home = tmp_path
    patcher = mock.patch("pathlib.Path.home", return_value=home)
    patcher.start()

    old_instance = ProfileManager._instance
    old_db_path = db_module.DB_PATH
    old_data_dir = app_module.DATA_DIR
    old_workout_dir = app_module.WORKOUT_DIR
    old_gpx_dir = app_module.GPX_DIR
    old_tp_plan_dir = tp.PLAN_DIR
    old_tp_workout_dir = tp.WORKOUT_DIR
    old_environ = {k: os.environ.get(k) for k in _ENV_KEYS}
    for attr in _ENV_KEYS:                 # kill stale shadows from other suites
        try:
            delattr(config, attr)
        except AttributeError:
            pass

    ProfileManager._instance = None
    app_module.DATA_DIR = home / ".domestique"

    pm = ProfileManager.get()
    pid = pm.create_profile("Pusher")
    pm.switch(pid)
    db_module._sync_stop.clear()

    workouts = home / "wk"
    workouts.mkdir()
    (workouts / "steady.zwo").write_text(STEADY_ZWO, encoding="utf-8")
    (workouts / "tempo_alt.zwo").write_text(TEMPO_ZWO, encoding="utf-8")
    (workouts / "sprint_end.zwo").write_text(SPRINT_END_ZWO, encoding="utf-8")
    real = REPO / "workouts" / "threshold_steady_56min.zwo"
    if real.exists():
        (workouts / "threshold_steady_56min.zwo").write_bytes(real.read_bytes())
    app_module.WORKOUT_DIR = workouts

    ns = types.SimpleNamespace(home=home, pm=pm, pid=pid, workouts=workouts,
                               client=TestClient(app_module.app))
    try:
        yield ns
    finally:
        patcher.stop()
        # Drop any armed debounce timer before restoring globals.
        t = app_module._icu_push_timer
        if t is not None:
            t.cancel()
        app_module._icu_push_timer = None
        tp.post_write_callback = None
        ProfileManager._instance = old_instance
        app_module.DATA_DIR = old_data_dir
        app_module.WORKOUT_DIR = old_workout_dir
        app_module.GPX_DIR = old_gpx_dir
        tp.PLAN_DIR = old_tp_plan_dir
        tp.WORKOUT_DIR = old_tp_workout_dir
        db_module.set_db_path(old_db_path)
        db_module._sync_stop.clear()
        app_module.clear_cache()
        for k, v in old_environ.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for attr in _ENV_KEYS:
            try:
                delattr(config, attr)
            except AttributeError:
                pass


def _sess(day, zwo="steady.zwo", stype="z2", **kw):
    s = {"day": day, "day_name": "X", "session_type": stype,
         "duration_min": 60, "tss_estimate": 50.0,
         "description": f"{stype} session", "zwo_file": zwo,
         "zwo_name": Path(zwo).stem if zwo else "", "status": "pending"}
    s.update(kw)
    return s


def _write_plan(sessions):
    days = sorted(s["day"] for s in sessions) or [TODAY.isoformat()]
    plan = {"goal": {"type": "ftp"}, "generated": "2026-07-01T00:00:00",
            "weeks": [{"week_num": 1, "start": days[0], "end": days[-1],
                       "phase": "base", "tss_target": 300,
                       "is_stepback": False, "sessions": sessions}]}
    pdir = Path(tp.PLAN_DIR)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "current_plan.json").write_text(json.dumps(plan), encoding="utf-8")


def _apikey(stub):
    stub.pm.save_env("i123", "KEY123")     # apikey auth → write-capable (G-F)


class FakeICU:
    """Stateful fake of the ICU calendar behind the engine's _http seam."""

    def __init__(self, events=None, fail=None):
        self.events = [dict(e) for e in (events or [])]
        self.calls = []
        self.fail = dict(fail or {})       # method → (status, body)
        self._next_id = 1000

    def snapshot(self):
        return json.loads(json.dumps(self.events, sort_keys=True))

    def ids(self):
        return sorted(str(e.get("external_id")) for e in self.events)

    def __call__(self, method, path, payload=None, timeout=30.0):
        self.calls.append((method, path, payload))
        if method in self.fail:
            return self.fail[method]
        if method == "GET":
            return 200, json.dumps(self.events).encode()
        if method == "POST":
            for ev in payload:
                cur = next((e for e in self.events
                            if e.get("external_id") == ev["external_id"]), None)
                if cur is not None:
                    cur.update(ev)
                else:
                    row = dict(ev)
                    row["id"] = self._next_id
                    self._next_id += 1
                    self.events.append(row)
            return 200, b"[]"
        if method == "PUT":
            gone = {d.get("external_id") for d in payload}
            self.events = [e for e in self.events
                           if e.get("external_id") not in gone]
            return 200, b"{}"
        raise AssertionError(f"unexpected method {method}")


@pytest.fixture()
def fake(monkeypatch):
    f = FakeICU()
    monkeypatch.setattr(icp, "_http", f)
    return f


def _eid(stub, day, n=0):
    return f"domestique:{stub.pid}:{day}:{n}"


# ---------------------------------------------------------------------------
# G1 — idempotency
# ---------------------------------------------------------------------------

class TestG1Idempotency:
    def test_second_reconcile_is_zero_changes(self, stub, fake):
        _apikey(stub)
        _write_plan([_sess(D1), _sess(D2, stype="tempo")])
        r1 = icp.reconcile()
        assert (r1["pushed"], r1["updated"], r1["deleted"]) == (2, 0, 0)
        assert not r1.get("error") and not r1.get("needs_reconnect")
        assert fake.ids() == sorted([_eid(stub, D1), _eid(stub, D2)])
        snap = fake.snapshot()

        r2 = icp.reconcile()
        assert (r2["pushed"], r2["updated"], r2["deleted"]) == (0, 0, 0)
        assert fake.snapshot() == snap      # calendar state untouched


# ---------------------------------------------------------------------------
# G2 — net state after dismiss / rematch / move; double-threshold day
# ---------------------------------------------------------------------------

class TestG2NetState:
    def test_dismiss_deletes_event(self, stub, fake):
        _apikey(stub)
        _write_plan([_sess(D1)])
        icp.reconcile()
        assert fake.ids() == [_eid(stub, D1)]
        _write_plan([_sess(D1, status="dismissed")])
        r = icp.reconcile()
        assert r["deleted"] == 1 and fake.events == []

    def test_rematch_updates_same_event(self, stub, fake):
        _apikey(stub)
        _write_plan([_sess(D1)])
        icp.reconcile()
        _write_plan([_sess(D1, zwo="tempo_alt.zwo", stype="tempo")])
        r = icp.reconcile()
        assert (r["pushed"], r["updated"], r["deleted"]) == (0, 1, 0)
        assert fake.ids() == [_eid(stub, D1)]           # same external_id
        ev = fake.events[0]
        assert ev["filename"] == "tempo_alt.zwo"
        assert ev["name"] == "tempo_alt"

    def test_move_old_day_gone_new_day_present(self, stub, fake):
        _apikey(stub)
        _write_plan([_sess(D1)])
        icp.reconcile()
        # Real move shape from _apply_move_session: the source day keeps a
        # GHOST row (rest, empty zwo, status "moved_from:<dst>") — the engine
        # must not push it AND must still sweep the old-day event.
        ghost = {"day": D1, "day_name": "X", "session_type": "rest",
                 "duration_min": 0, "tss_estimate": 0,
                 "description": f"Moved to {D3}", "zwo_file": "",
                 "zwo_name": "", "status": f"moved_from:{D3}",
                 "user_moved": False, "moved_from": "",
                 "completion_matches": None, "dismissed_at": ""}
        _write_plan([ghost, _sess(D3, user_moved=True, moved_from=D1)])
        r = icp.reconcile()
        assert r["pushed"] == 1 and r["deleted"] == 1
        assert fake.ids() == [_eid(stub, D3)]

    def test_double_threshold_day_two_events(self, stub, fake):
        _apikey(stub)
        am = _sess(D1, stype="threshold", is_double_threshold_pair=True,
                   am_or_pm="am")
        pm_ = _sess(D1, zwo="tempo_alt.zwo", stype="threshold",
                    is_double_threshold_pair=True, am_or_pm="pm")
        _write_plan([am, pm_])
        r = icp.reconcile()
        assert r["pushed"] == 2
        assert fake.ids() == sorted([_eid(stub, D1, 0), _eid(stub, D1, 1)])


# ---------------------------------------------------------------------------
# G3 — format by mode
# ---------------------------------------------------------------------------

class TestG3Format:
    def test_power_mode_pushes_zwo_byte_identical(self, stub, fake):
        _apikey(stub)
        _write_plan([_sess(D1)])
        r = icp.reconcile()
        assert r["pushed"] == 1
        post = next(c for c in fake.calls if c[0] == "POST")
        ev = post[2][0]
        disk = (stub.workouts / "steady.zwo").read_bytes()
        assert base64.b64decode(ev["file_contents_base64"]) == disk
        assert ev["filename"] == "steady.zwo"
        assert ev["category"] == "WORKOUT"
        assert ev["start_date_local"] == f"{D1}T00:00:00"
        assert ev["external_id"] == _eid(stub, D1)

    def test_hr_mode_fit_carries_heart_rate_targets(self, stub, fake):
        fitparse = pytest.importorskip("fitparse")
        if not (stub.workouts / "threshold_steady_56min.zwo").exists():
            pytest.skip("bundled zwo fixture missing")
        _apikey(stub)
        stub.pm._athlete.update(
            {"target_mode": "hr", "lthr": 160, "max_hr": 185})
        _write_plan([_sess(D1, zwo="threshold_steady_56min.zwo",
                           stype="threshold")])
        r = icp.reconcile()
        assert r["pushed"] == 1 and not r.get("needs_lthr")
        ev = next(c for c in fake.calls if c[0] == "POST")[2][0]
        assert ev["filename"].endswith(".fit")
        fit_bytes = base64.b64decode(ev["file_contents_base64"])
        steps = [{f.name: f.value for f in m.fields}
                 for m in fitparse.FitFile(fit_bytes).get_messages("workout_step")]
        assert steps
        hr_steps = [s for s in steps if s.get("target_type") == "heart_rate"]
        assert hr_steps, "hr-mode FIT carries no HEART_RATE targets"
        # C8: bpm encoded +100 → raw values sit above 100.
        assert all(s["custom_target_heart_rate_high"] > 100 for s in hr_steps)

    def test_hr_mode_broken_lthr_skips_needs_lthr(self, stub, fake):
        _apikey(stub)
        stub.pm._athlete.update({"target_mode": "hr"})   # no lthr set
        # A previously pushed event for this slot must SURVIVE the sweep —
        # a config hiccup never wipes the athlete's calendar.
        fake.events.append({"id": 7, "external_id": _eid(stub, D1),
                            "start_date_local": f"{D1}T00:00:00",
                            "name": "old", "category": "WORKOUT"})
        _write_plan([_sess(D1)])
        r = icp.reconcile()
        assert r.get("needs_lthr") is True
        assert {"day": D1, "reason": "needs_lthr"} in r["skipped"]
        assert not any(c[0] == "POST" for c in fake.calls)   # nothing pushed
        assert fake.ids() == [_eid(stub, D1)]                # event kept


# ---------------------------------------------------------------------------
# G4 — missing write scope
# ---------------------------------------------------------------------------

class TestG4Scope:
    def test_unstamped_oauth_needs_reconnect_no_network(self, stub, fake):
        # Legacy OAuth connection: token but no ICU_GRANTED_SCOPES stamp.
        stub.pm.save_icu_token("TOK", "i123")
        _write_plan([_sess(D1)])
        r = icp.reconcile()
        assert r.get("needs_reconnect") is True
        assert fake.calls == []                    # pre-check, zero transport

    def test_scope_403_body_needs_reconnect_no_exception(self, stub, fake):
        _apikey(stub)
        fake.fail["GET"] = (403, b'{"error":"missing scope CALENDAR:WRITE"}')
        _write_plan([_sess(D1)])
        r = icp.reconcile()
        assert r.get("needs_reconnect") is True
        assert "error" not in r

    def test_push_endpoint_never_500s(self, stub, fake):
        _apikey(stub)
        fake.fail["GET"] = (403, b"missing scope")
        _write_plan([_sess(D1)])
        resp = stub.client.post("/api/icu/push", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["needs_reconnect"] is True and body["ok"] is False

    def test_stamped_oauth_with_write_scope_pushes(self, stub, fake):
        stub.pm.save_icu_token(
            "TOK", "i123",
            granted_scopes="ACTIVITY:READ,CALENDAR:READ,CALENDAR:WRITE")
        _write_plan([_sess(D1)])
        r = icp.reconcile()
        assert r["pushed"] == 1 and not r.get("needs_reconnect")


# ---------------------------------------------------------------------------
# G5 / G-B — sweep safety on a mixed calendar
# ---------------------------------------------------------------------------

class TestG5SweepSafety:
    def test_mixed_calendar_only_ours_deleted(self, stub, fake):
        _apikey(stub)
        users_own = {"id": 1, "external_id": None, "name": "My own ride",
                     "start_date_local": f"{D1}T00:00:00", "category": "WORKOUT"}
        third_party = {"id": 2, "external_id": "trainerroad:abc",
                       "start_date_local": f"{D1}T00:00:00", "name": "TR",
                       "category": "WORKOUT"}
        foreign_dom = {"id": 3, "external_id": f"domestique:other-profile:{D1}:0",
                       "start_date_local": f"{D1}T00:00:00", "name": "Other",
                       "category": "WORKOUT"}
        ours_stale = {"id": 4, "external_id": _eid(stub, D2),
                      "start_date_local": f"{D2}T00:00:00", "name": "stale",
                      "category": "WORKOUT"}
        fake.events += [users_own, third_party, foreign_dom, ours_stale]
        _write_plan([_sess(D1)])                   # D2 session no longer exists
        with mock.patch.object(icp._log, "warning") as warn:
            r = icp.reconcile()
        assert r["deleted"] == 1
        ids = fake.ids()
        assert _eid(stub, D2) not in ids                     # ours-stale gone
        assert "None" in ids or None in [e.get("external_id") for e in fake.events]
        assert "trainerroad:abc" in ids
        assert f"domestique:other-profile:{D1}:0" in ids     # G-B: never touched
        assert _eid(stub, D1) in ids
        assert any("foreign" in str(c) for c in warn.call_args_list)

    def test_sweep_skipped_when_upsert_fails(self, stub, fake):
        _apikey(stub)
        stale = {"id": 9, "external_id": _eid(stub, D2), "name": "stale",
                 "start_date_local": f"{D2}T00:00:00", "category": "WORKOUT"}
        fake.events.append(stale)
        fake.fail["POST"] = (500, b"boom")
        _write_plan([_sess(D1)])
        r = icp.reconcile()
        assert r.get("error") == "http_500"
        assert not any(c[0] == "PUT" for c in fake.calls)    # no sweep
        assert _eid(stub, D2) in fake.ids()


# ---------------------------------------------------------------------------
# G-E — trailing OPEN step
# ---------------------------------------------------------------------------

class TestGETrailingOpen:
    def test_fit_ending_open_is_skipped_and_prior_event_kept(self, stub, fake):
        _apikey(stub)
        stub.pm._athlete.update(
            {"target_mode": "hr", "lthr": 160, "max_hr": 185})
        fake.events.append({"id": 5, "external_id": _eid(stub, D1),
                            "start_date_local": f"{D1}T00:00:00",
                            "name": "prior", "category": "WORKOUT"})
        _write_plan([_sess(D1, zwo="sprint_end.zwo", stype="sprint")])
        r = icp.reconcile()
        assert {"day": D1, "reason": "trailing_open"} in r["skipped"]
        assert not any(c[0] == "POST" for c in fake.calls)
        assert fake.ids() == [_eid(stub, D1)]      # protected from the sweep


# ---------------------------------------------------------------------------
# G-H — window starts today
# ---------------------------------------------------------------------------

class TestGHWindow:
    def test_past_day_event_untouched(self, stub, fake):
        _apikey(stub)
        past = {"id": 6, "external_id": _eid(stub, YESTERDAY),
                "start_date_local": f"{YESTERDAY}T00:00:00",
                "name": "done ride", "category": "WORKOUT"}
        fake.events.append(past)                   # server returns it anyway
        _write_plan([_sess(D1)])
        r = icp.reconcile()
        assert r["deleted"] == 0
        assert _eid(stub, YESTERDAY) in fake.ids()
        get_call = next(c for c in fake.calls if c[0] == "GET")
        assert f"oldest={TODAY.isoformat()}" in get_call[1]

    def test_past_session_not_pushed(self, stub, fake):
        _apikey(stub)
        _write_plan([_sess(YESTERDAY), _sess(D1)])
        r = icp.reconcile()
        assert r["pushed"] == 1
        assert fake.ids() == [_eid(stub, D1)]


# ---------------------------------------------------------------------------
# G-A — toggle-off final sweep + disconnect sweep-before-purge
# ---------------------------------------------------------------------------

class TestGAOffboarding:
    def test_toggle_off_runs_final_sweep(self, stub, fake):
        _apikey(stub)
        stub.pm.save_prefs({"icu_calendar_sync": True})
        fake.events += [
            {"id": 1, "external_id": _eid(stub, D1), "name": "a",
             "start_date_local": f"{D1}T00:00:00", "category": "WORKOUT"},
            {"id": 2, "external_id": _eid(stub, D2), "name": "b",
             "start_date_local": f"{D2}T00:00:00", "category": "WORKOUT"},
            {"id": 3, "external_id": "keep-me", "name": "user",
             "start_date_local": f"{D1}T00:00:00", "category": "WORKOUT"},
        ]
        resp = stub.client.post("/api/icu/push", json={"sync": False})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True and body["deleted"] == 2
        assert stub.pm.prefs.get("icu_calendar_sync") is False
        assert fake.ids() == ["keep-me"]

    def test_toggle_on_requires_write_ok(self, stub, fake):
        stub.pm.save_icu_token("TOK", "i123")      # unstamped oauth
        resp = stub.client.post("/api/icu/push", json={"sync": True})
        body = resp.json()
        assert body["ok"] is False and body["needs_reconnect"] is True
        assert not stub.pm.prefs.get("icu_calendar_sync")

    def test_disconnect_sweeps_before_purge(self, stub):
        _apikey(stub)
        order = []
        with mock.patch.object(icp, "sweep_all",
                               side_effect=lambda *a, **k: order.append("sweep") or {}), \
             mock.patch.object(db_module, "purge_profile_data",
                               side_effect=lambda pid: order.append("purge")):
            resp = stub.client.post("/api/icu/disconnect")
        assert resp.status_code == 200 and resp.json()["ok"] is True
        assert order == ["sweep", "purge"]


# ---------------------------------------------------------------------------
# write_ok on the connection payload (G-F + stamp backfill rule)
# ---------------------------------------------------------------------------

class TestConnectionWriteOk:
    def test_apikey_always_write_capable(self, stub):
        _apikey(stub)
        c = stub.client.get("/api/icu/connection").json()
        assert c["method"] == "apikey" and c["write_ok"] is True

    def test_oauth_without_stamp_not_write_capable(self, stub):
        stub.pm.save_icu_token("TOK", "i123")
        c = stub.client.get("/api/icu/connection").json()
        assert c["method"] == "oauth" and c["write_ok"] is False

    def test_oauth_with_write_stamp_write_capable(self, stub):
        stub.pm.save_icu_token(
            "TOK", "i123",
            granted_scopes="ACTIVITY:READ,WELLNESS:READ,CALENDAR:WRITE")
        c = stub.client.get("/api/icu/connection").json()
        assert c["method"] == "oauth" and c["write_ok"] is True

    def test_callback_stamps_granted_scopes(self, stub):
        app_module._icu_oauth_states["SP"] = {"profile_id": stub.pid,
                                              "ts": 9e12}

        class _Resp:
            status_code = 200

            def json(self):
                return {"access_token": "ATOK",
                        "athlete": {"id": "i77", "name": "Z"},
                        "scope": "ACTIVITY:READ,CALENDAR:READ,CALENDAR:WRITE"}

        with mock.patch("httpx.post", return_value=_Resp()):
            r = stub.client.get("/oauth/icu/callback?code=C&state=SP",
                                follow_redirects=False)
        assert "icu=connected" in r.headers["location"]
        assert "CALENDAR:WRITE" in stub.pm.icu_granted_scopes
        env = (stub.home / ".domestique" / "profiles" / stub.pid / ".env")
        assert "ICU_GRANTED_SCOPES=ACTIVITY:READ,CALENDAR:READ,CALENDAR:WRITE" \
            in env.read_text(encoding="utf-8")
        # Disconnect drops the stamp with the token.
        stub.pm.save_icu_token("", None)
        assert stub.pm.icu_granted_scopes == ""


# ---------------------------------------------------------------------------
# Plan-write hook + debounce wiring
# ---------------------------------------------------------------------------

class TestPlanWriteHook:
    def test_atomic_write_plan_fires_post_write_callback(self, stub, tmp_path):
        seen = []
        tp.post_write_callback = seen.append
        try:
            p = tmp_path / "current_plan.json"
            tp.atomic_write_plan(p, {"weeks": []})
        finally:
            tp.post_write_callback = None
        assert seen == [p]

    def test_callback_failure_never_breaks_the_write(self, stub, tmp_path):
        def boom(_p):
            raise RuntimeError("hook exploded")
        tp.post_write_callback = boom
        try:
            p = tmp_path / "current_plan.json"
            tp.atomic_write_plan(p, {"weeks": [1]})
        finally:
            tp.post_write_callback = None
        assert json.loads(p.read_text(encoding="utf-8")) == {"weeks": [1]}

    def test_debounce_arms_only_when_toggle_on_and_coalesces(self, stub):
        plan_path = Path(tp.PLAN_DIR) / "current_plan.json"
        app_module._icu_push_schedule_debounced(plan_path)
        assert app_module._icu_push_timer is None          # pref OFF → no-op
        stub.pm.save_prefs({"icu_calendar_sync": True})
        try:
            app_module._icu_push_schedule_debounced(plan_path)
            t1 = app_module._icu_push_timer
            assert t1 is not None
            app_module._icu_push_schedule_debounced(plan_path)
            t2 = app_module._icu_push_timer
            assert t2 is not None and t2 is not t1         # re-armed, coalesced
            # A non-plan write never schedules.
            t2.cancel()
            app_module._icu_push_timer = None
            app_module._icu_push_schedule_debounced(
                Path(tp.PLAN_DIR) / "something_else.json")
            assert app_module._icu_push_timer is None
        finally:
            t = app_module._icu_push_timer
            if t is not None:
                t.cancel()
            app_module._icu_push_timer = None


class TestDebouncedFireTime:
    """The runner half of the debounce (was untested — only scheduling was)."""

    def test_fires_reconcile_when_profile_unchanged(self, stub, monkeypatch):
        stub.pm.save_prefs({"icu_calendar_sync": True})
        calls = []
        monkeypatch.setattr(icp, "reconcile", lambda: calls.append(1) or {})
        app_module._icu_push_debounced_run(stub.pid)
        assert calls == [1]

    def test_aborts_on_profile_switch(self, stub, monkeypatch):
        stub.pm.save_prefs({"icu_calendar_sync": True})
        calls = []
        monkeypatch.setattr(icp, "reconcile", lambda: calls.append(1) or {})
        app_module._icu_push_debounced_run("some-other-profile")
        assert calls == []                  # TOCTOU guard: scheduler pid gone

    def test_aborts_when_toggled_off_before_fire(self, stub, monkeypatch):
        calls = []
        monkeypatch.setattr(icp, "reconcile", lambda: calls.append(1) or {})
        app_module._icu_push_debounced_run(stub.pid)   # pref OFF
        assert calls == []

    def test_toggle_off_endpoint_cancels_pending_timer(self, stub, fake):
        _apikey(stub)
        stub.pm.save_prefs({"icu_calendar_sync": True})
        app_module._icu_push_schedule_debounced(
            Path(tp.PLAN_DIR) / "current_plan.json")
        assert app_module._icu_push_timer is not None
        r = stub.client.post("/api/icu/push", json={"sync": False})
        assert r.status_code == 200
        assert app_module._icu_push_timer is None      # no post-sweep re-push


class TestDailyFromSync:
    """db.post_sync_callback half of D3b — one reconcile per calendar day."""

    def test_once_per_day_latch(self, stub, monkeypatch):
        stub.pm.save_prefs({"icu_calendar_sync": True})
        calls = []
        monkeypatch.setattr(icp, "reconcile", lambda: calls.append(1) or {})
        monkeypatch.setattr(app_module, "_icu_push_last_daily", None)
        app_module._icu_push_daily_from_sync()
        app_module._icu_push_daily_from_sync()
        assert calls == [1]                 # second pass same day = no-op

    def test_disabled_toggle_never_runs(self, stub, monkeypatch):
        calls = []
        monkeypatch.setattr(icp, "reconcile", lambda: calls.append(1) or {})
        monkeypatch.setattr(app_module, "_icu_push_last_daily", None)
        app_module._icu_push_daily_from_sync()
        assert calls == [] and app_module._icu_push_last_daily is None

    def test_transient_error_retries_next_pass(self, stub, monkeypatch):
        stub.pm.save_prefs({"icu_calendar_sync": True})
        monkeypatch.setattr(icp, "reconcile", lambda: {"error": "net_down"})
        monkeypatch.setattr(app_module, "_icu_push_last_daily", None)
        app_module._icu_push_daily_from_sync()
        assert app_module._icu_push_last_daily is None   # latch released

    def test_sync_loop_calls_post_sync_callback(self, stub, monkeypatch):
        """The db-side hook actually fires after a successful pass."""
        fired = []
        monkeypatch.setattr(db_module, "post_sync_callback", lambda: fired.append(1))
        monkeypatch.setattr(db_module, "run_sync", lambda days=90: None)
        monkeypatch.setattr(db_module, "_auth_disabled", False)
        db_module._sync_stop.clear()
        # One loop iteration: let the post-run wait() see the stop event.
        orig_wait = db_module._sync_stop.wait
        def _stop_now(_t):
            db_module._sync_stop.set()
            return True
        monkeypatch.setattr(db_module._sync_stop, "wait", _stop_now)
        try:
            db_module._sync_loop(interval_sec=1)
        finally:
            monkeypatch.setattr(db_module._sync_stop, "wait", orig_wait)
            db_module._sync_stop.clear()
        assert fired == [1]


class TestSweepFailClosed:
    """G-H hardening: an our-prefix event with a missing/garbled date must
    never enter the deletable set (fail closed)."""

    def test_unparseable_date_not_deleted(self, stub, fake):
        _apikey(stub)
        fake.events = [
            {"external_id": _eid(stub, D1), "start_date_local": None,
             "name": "no date", "id": 1},
            {"external_id": _eid(stub, D2), "start_date_local": "garbage",
             "name": "bad date", "id": 2},
        ]
        _write_plan([])                      # nothing desired
        icp.reconcile()
        assert len(fake.events) == 2         # both survive the sweep

    def test_unmatched_session_keeps_prior_event(self, stub, fake):
        _apikey(stub)
        _write_plan([_sess(D1)])
        icp.reconcile()
        assert fake.ids() == [_eid(stub, D1)]
        _write_plan([_sess(D1, zwo="")])     # transiently lost its match
        r = icp.reconcile()
        assert r["deleted"] == 0
        assert fake.ids() == [_eid(stub, D1)]
        assert {"day": D1, "reason": "unmatched"} in r["skipped"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
