"""Send-to-intervals.icu-calendar workout button — calendar-push (2026-07-07).

Covers POST /api/calendar/push-workout (library + planner) and the
_build_event extraction. Hermetic: stubbed HOME (fresh ProfileManager under
tmp_path) with the ICU transport mocked at the engine's ONE seam
(``icu_calendar_push._http``) — the conftest network block stays king.

The stub / FakeICU harness mirrors tests/test_icu_push.py so the two suites
stay in lockstep (same seam, same profile bootstrap).

Contract coverage:
  GA1  a domestique-manual: entry survives a full reconcile()+sweep — NOT
       deleted, NOT counted foreign (distinct root); + the locked startswith
       unit fact.
  GA2  planner push carries the EXACT domestique:<pid>:<day>:<n> _desired_events
       would produce → a following reconcile makes no duplicate.
  GA3  write_ok False → needs_reconnect, zero upsert; the icu_calendar_sync
       PREF being OFF does NOT block a manual push (D2).
  GA4  past / garbage / >+365 date → {error}; today & +365 accepted.
  GA5  _build_event is byte-identical to the pre-extraction _desired_events
       output (dict equality + JSON serialization + the 6-key contract).
  O2   HR-mode profile + broken LTHR → needs_lthr, zero upsert (both sources).
  sec  a client "../../etc/passwd" zwo_file is rejected before any read.
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
PLUS_365 = (TODAY + timedelta(days=365)).isoformat()
PLUS_366 = (TODAY + timedelta(days=366)).isoformat()

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


# ---------------------------------------------------------------------------
# Stubbed-HOME fixture (pattern: tests/test_icu_push.py)
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
    real = REPO / "workouts" / "threshold_2x3min-3min_95pct_56min.zwo"
    if real.exists():
        (workouts / "threshold_2x3min-3min_95pct_56min.zwo").write_bytes(real.read_bytes())
    app_module.WORKOUT_DIR = workouts

    ns = types.SimpleNamespace(home=home, pm=pm, pid=pid, workouts=workouts,
                               client=TestClient(app_module.app))
    try:
        yield ns
    finally:
        patcher.stop()
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


class FakeICU:
    """Stateful fake of the ICU calendar behind the engine's _http seam."""

    def __init__(self, events=None, fail=None):
        self.events = [dict(e) for e in (events or [])]
        self.calls = []
        self.fail = dict(fail or {})       # method → (status, body)
        self._next_id = 1000

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


def _eid(stub, day, n=0):
    return f"domestique:{stub.pid}:{day}:{n}"


def _push(stub, **body):
    return stub.client.post("/api/calendar/push-workout", json=body).json()


# ---------------------------------------------------------------------------
# GA1 — sweep-immunity of a manual (library) entry
# ---------------------------------------------------------------------------

class TestGA1SweepImmunity:
    def test_manual_entry_survives_reconcile_sweep(self, stub, fake):
        _apikey(stub)
        # Create a real manual entry through the endpoint.
        r = _push(stub, source="library", zwo_file="steady.zwo",
                  date=D1, name="Manual Steady")
        assert r["ok"] is True and r["pushed"] == 1 and r["event_date"] == D1
        manual = [e for e in fake.ids() if e.startswith(icp._MANUAL_ID_ROOT)]
        assert len(manual) == 1

        # Reconcile a plan that does NOT contain this manual entry: it must be
        # neither swept nor counted as a foreign domestique: prefix.
        _write_plan([_sess(D2, zwo="tempo_alt.zwo", stype="tempo")])
        with mock.patch.object(icp._log, "warning") as warn:
            res = icp.reconcile()
        assert res["deleted"] == 0
        assert manual[0] in fake.ids()                    # survived the sweep
        assert not any("foreign" in str(c) for c in warn.call_args_list)

    def test_manual_root_startswith_id_root_is_false(self):
        # Load-bearing boundary (the ":" at index 10) — locked so no future
        # switch of _ours_in_window to split/regex silently re-includes manuals.
        assert not "domestique-manual:x".startswith(icp._ID_ROOT)
        assert icp._MANUAL_ID_ROOT == "domestique-manual:"


# ---------------------------------------------------------------------------
# GA2 — planner push reuses the exact _desired_events ext_id (no duplicate)
# ---------------------------------------------------------------------------

class TestGA2PlannerExtId:
    def test_planner_push_matches_desired_and_no_dup(self, stub, fake):
        _apikey(stub)
        _write_plan([_sess(D1)])
        r = _push(stub, source="planner", zwo_file="steady.zwo", date=D1)
        assert r["ok"] is True and r["pushed"] == 1 and r["event_date"] == D1

        post = next(c for c in fake.calls if c[0] == "POST")
        ev = post[2][0]
        assert ev["external_id"] == _eid(stub, D1)        # domestique:<pid>:D1:0

        # A subsequent full reconcile finds it already present → no duplicate.
        res = icp.reconcile()
        assert (res["pushed"], res["updated"], res["deleted"]) == (0, 0, 0)
        assert fake.ids() == [_eid(stub, D1)]

    def test_planner_push_beyond_horizon_still_works(self, stub, fake):
        # O1: a day beyond the 14-day auto-sync window is still pushable (the
        # endpoint extends the horizon to cover it).
        _apikey(stub)
        far = (TODAY + timedelta(days=40)).isoformat()
        _write_plan([_sess(far)])
        r = _push(stub, source="planner", zwo_file="steady.zwo", date=far)
        assert r["ok"] is True and r["pushed"] == 1
        assert fake.ids() == [_eid(stub, far)]

    def test_planner_unmatched_day_is_not_pushable(self, stub, fake):
        _apikey(stub)
        _write_plan([_sess(D1)])                          # nothing on D2
        r = _push(stub, source="planner", zwo_file="steady.zwo", date=D2)
        assert r.get("error") == "not a pushable planned session"
        assert not any(c[0] == "POST" for c in fake.calls)


# ---------------------------------------------------------------------------
# GA3 — write scope + pref independence
# ---------------------------------------------------------------------------

class TestGA3ScopeAndPref:
    def test_write_ok_false_needs_reconnect_no_upsert(self, stub, fake):
        stub.pm.save_icu_token("TOK", "i123")             # unstamped oauth
        r = _push(stub, source="library", zwo_file="steady.zwo",
                  date=D1, name="X")
        assert r.get("needs_reconnect") is True
        assert not any(c[0] == "POST" for c in fake.calls)

    def test_manual_push_ignores_sync_pref(self, stub, fake):
        _apikey(stub)
        assert not stub.pm.prefs.get("icu_calendar_sync")  # default OFF (D2)
        r = _push(stub, source="library", zwo_file="steady.zwo",
                  date=D1, name="X")
        assert r["ok"] is True and r["pushed"] == 1
        assert any(c[0] == "POST" for c in fake.calls)     # upserted anyway


# ---------------------------------------------------------------------------
# GA4 — date validation (library)
# ---------------------------------------------------------------------------

class TestGA4DateValidation:
    @pytest.mark.parametrize("d", [TODAY.isoformat(), D1, PLUS_365])
    def test_accepted_dates(self, stub, fake, d):
        _apikey(stub)
        r = _push(stub, source="library", zwo_file="steady.zwo",
                  date=d, name="X")
        assert r.get("ok") is True and r["event_date"] == d

    @pytest.mark.parametrize("d", [YESTERDAY, "not-a-date", "", PLUS_366])
    def test_rejected_dates(self, stub, fake, d):
        _apikey(stub)
        r = _push(stub, source="library", zwo_file="steady.zwo",
                  date=d, name="X")
        assert r.get("error") and not r.get("ok")
        assert not any(c[0] == "POST" for c in fake.calls)  # nothing pushed


# ---------------------------------------------------------------------------
# O2 — HR-mode profile with a broken/missing LTHR
# ---------------------------------------------------------------------------

class TestO2NeedsLthr:
    @pytest.mark.parametrize("source", ["library", "planner"])
    def test_hr_mode_broken_lthr_needs_lthr_no_upsert(self, stub, fake, source):
        _apikey(stub)
        stub.pm._athlete.update({"target_mode": "hr"})    # no lthr set
        if source == "planner":
            _write_plan([_sess(D1)])
        r = _push(stub, source=source, zwo_file="steady.zwo",
                  date=D1, name="X")
        assert r.get("needs_lthr") is True
        assert not any(c[0] == "POST" for c in fake.calls)


# ---------------------------------------------------------------------------
# security — client-supplied path traversal (amendment 5)
# ---------------------------------------------------------------------------

class TestSafePath:
    def test_library_traversal_rejected_before_read(self, stub, fake):
        _apikey(stub)
        r = _push(stub, source="library", zwo_file="../../etc/passwd",
                  date=D1, name="X")
        assert r.get("error") == "invalid_workout_file"
        assert not r.get("ok")
        assert not any(c[0] == "POST" for c in fake.calls)


# ---------------------------------------------------------------------------
# GA5 — _build_event is byte-identical to the pre-extraction inline path
# ---------------------------------------------------------------------------

class TestGA5BuildEventByteIdentical:
    def test_build_event_equals_desired_events_output(self, stub, fake):
        _apikey(stub)
        s = _sess(D1)
        _write_plan([s])
        wd = Path(app_module.WORKOUT_DIR)
        cls = icp._load_classifications(wd)

        desired, _, _ = icp._desired_events(
            stub.pm, icp._load_plan(), TODAY, 14, stub.pid)
        assert len(desired) == 1

        ev, reason = icp._build_event(
            s, _eid(stub, D1), stub.pm, cls, wd, False, True)
        assert reason is None
        # Dict equality AND identical JSON serialization = byte-identical event.
        assert ev == desired[0]
        assert json.dumps(ev) == json.dumps(desired[0])

    def test_build_event_six_key_contract_for_known_zwo(self, stub, fake):
        wd = Path(app_module.WORKOUT_DIR)
        s = _sess(D1)
        ev, reason = icp._build_event(
            s, _eid(stub, D1), stub.pm, {}, wd, False, True)
        assert reason is None
        assert list(ev.keys()) == [
            "start_date_local", "category", "name",
            "external_id", "filename", "file_contents_base64"]
        assert ev["start_date_local"] == f"{D1}T00:00:00"
        assert ev["category"] == "WORKOUT"
        assert ev["external_id"] == _eid(stub, D1)
        assert ev["filename"] == "steady.zwo"
        disk = (stub.workouts / "steady.zwo").read_bytes()
        assert base64.b64decode(ev["file_contents_base64"]) == disk

    def test_build_event_reports_skip_reasons(self, stub, fake):
        wd = Path(app_module.WORKOUT_DIR)
        # unmatched (no zwo)
        ev, reason = icp._build_event(
            _sess(D1, zwo=""), _eid(stub, D1), stub.pm, {}, wd, False, True)
        assert ev is None and reason == "unmatched"
        # file_missing (named but absent)
        ev, reason = icp._build_event(
            _sess(D1, zwo="ghost.zwo"), _eid(stub, D1), stub.pm, {}, wd,
            False, True)
        assert ev is None and reason == "file_missing"
        # needs_lthr (hr mode, broken invariant)
        ev, reason = icp._build_event(
            _sess(D1), _eid(stub, D1), stub.pm, {}, wd, True, False)
        assert ev is None and reason == "needs_lthr"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
