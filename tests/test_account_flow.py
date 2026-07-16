"""Account/identity flow — FLOW half of IP_ACCOUNT_FIXES (v3.0.0).

Covers the locked contract items owned by app.py + templates:
  A2  fresh-boot → wizard → dashboard; bootstrapped-flag redirect rule;
      abandon-mid-wizard atomicity (validate-all-then-write, no partial creds)
  A4  disconnect / reconnect-different-athlete purge (db.purge_profile_data)
  A5  wizard invariants: lthr<max_hr 400+detail, extras persist, ONE range
      table (server == client == /api/setup/limits), target-mode gate,
      untouched defaults write NOTHING
  A9  concurrent OAuth flows for two profiles (either order) + deleted-profile
      callback → the token binds to the STATE's profile, never the active one
  A13 app-side sync-write-gate wiring (_sync_task_snapshot is the exact
      3-tuple db.sync_write_gate checks)

Every filesystem-touching test runs against a stubbed HOME (fresh
ProfileManager singleton under tmp_path) — the real ~/.domestique is never
read or written.
"""
from __future__ import annotations

import json
import os
import re
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import db as db_module  # noqa: E402
import app as app_module  # noqa: E402
from profile_manager import ProfileManager  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
_ENV_KEYS = ("ICU_ATHLETE_ID", "ICU_API_KEY", "ICU_ACCESS_TOKEN")


# ---------------------------------------------------------------------------
# Stubbed-HOME fixture (pattern: scratchpad probe — never the real HOME)
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
    old_environ = {k: os.environ.get(k) for k in _ENV_KEYS}
    # Isolation fix (post-2fbfeb62): ProfileManager activation rewrites
    # training_planner.PLAN_DIR / WORKOUT_DIR to the active profile's dirs
    # (profile_manager.py:658) — this fixture activated profiles under a
    # tmp_path home and never restored those TP globals, so every LATER
    # test in the same worker process read/wrote plans in a DELETED tmp
    # dir (the "future dismissed day missing" parallel-gate red in
    # test_recalc_preserves_state, and the sequential hermetic-home escape
    # via app._plan_dir()).
    import training_planner as _tp
    old_tp_plan_dir = _tp.PLAN_DIR
    old_tp_workout_dir = _tp.WORKOUT_DIR

    ProfileManager._instance = None
    app_module.DATA_DIR = home / ".domestique"
    app_module._icu_oauth_states.clear()

    pm = ProfileManager.get()
    ns = types.SimpleNamespace(home=home, pm=pm,
                               client=TestClient(app_module.app))
    try:
        yield ns
    finally:
        patcher.stop()
        ProfileManager._instance = old_instance
        app_module.DATA_DIR = old_data_dir
        app_module.WORKOUT_DIR = old_workout_dir
        app_module.GPX_DIR = old_gpx_dir
        db_module.set_db_path(old_db_path)
        db_module._sync_stop.clear()
        app_module._icu_oauth_states.clear()
        app_module.clear_cache()
        _tp.PLAN_DIR = old_tp_plan_dir
        _tp.WORKOUT_DIR = old_tp_workout_dir
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


def _active_profile(stub, name="Solo"):
    """Create + activate a profile; returns its id. Clears the stop event the
    switch sets (restart_sync normally re-arms it via the app's on_switch
    callback, which isn't registered without the lifespan)."""
    pid = stub.pm.create_profile(name)
    stub.pm.switch(pid)
    db_module._sync_stop.clear()
    return pid


def _athlete_on_disk(stub, pid):
    p = stub.home / ".domestique" / "profiles" / pid / "athlete.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _env_on_disk(stub, pid):
    p = stub.home / ".domestique" / "profiles" / pid / ".env"
    out = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


class _TokenResp:
    """Fake ICU token-exchange response; athlete derived from the code."""
    status_code = 200

    def __init__(self, code, athlete_id=None, name=None):
        self._code = code
        self._athlete_id = athlete_id if athlete_id is not None else "i-" + code
        self._name = name if name is not None else "Rider " + code

    def json(self):
        return {"access_token": "TOK_" + self._code,
                "athlete": {"id": self._athlete_id, "name": self._name}}


def _fake_exchange(**overrides):
    def _post(url, data=None, **kw):
        code = (data or {}).get("code", "?")
        return _TokenResp(code, **overrides)
    return _post


# ---------------------------------------------------------------------------
# A2 — onboarding reachability + wizard atomicity
# ---------------------------------------------------------------------------

class TestFirstRun:
    def test_fresh_boot_redirects_to_setup(self, stub):
        # Empty HOME → no active profile → / must land in the wizard, even
        # though the repo/bundle ships .setup_complete (AC4: the bundle-marker
        # fallback is deleted; the module-load constant is gone with it).
        assert not hasattr(app_module, "SETUP_MARKER")
        r = stub.client.get("/", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert r.headers["location"] == "/setup"

    def test_setup_status_reads_data_dir_only(self, stub):
        assert stub.client.get("/api/setup/status").json() == {"complete": False}
        marker = app_module._setup_marker()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
        assert stub.client.get("/api/setup/status").json() == {"complete": True}

    def test_bootstrapped_profile_redirects_until_setup_complete(self, stub):
        # AC4 rule (flag-only): redirect while active profile is
        # bootstrapped==true AND setup not complete; stop once complete.
        pid = _active_profile(stub)
        for p in stub.pm._registry["profiles"]:
            if p["id"] == pid:
                p["bootstrapped"] = True
        stub.pm._save_registry()
        r = stub.client.get("/", follow_redirects=False)
        assert r.status_code in (302, 307) and r.headers["location"] == "/setup"
        marker = app_module._setup_marker()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
        assert stub.client.get("/", follow_redirects=False).status_code == 200

    def test_real_install_never_redirects(self, stub):
        # create_profile-made (not bootstrapped), no marker → dashboard, and
        # the page carries the active profile id for AC2d localStorage keys.
        pid = _active_profile(stub)
        r = stub.client.get("/", follow_redirects=False)
        assert r.status_code == 200
        assert f"__ACTIVE_PROFILE_ID = '{pid}'" in r.text

    @pytest.mark.skipif(not hasattr(ProfileManager, "clear_bootstrapped"),
                        reason="CORE's pm.clear_bootstrapped() not landed yet")
    def test_wizard_save_clears_bootstrapped(self, stub):
        pid = _active_profile(stub)
        for p in stub.pm._registry["profiles"]:
            if p["id"] == pid:
                p["bootstrapped"] = True
        stub.pm._save_registry()
        r = stub.client.post("/api/setup/save", json={"ftp": 275})
        assert r.status_code == 200, r.text
        entry = next(p for p in stub.pm.list_profiles() if p["id"] == pid)
        assert not entry.get("bootstrapped")
        assert stub.client.get("/", follow_redirects=False).status_code == 200

    def test_setup_save_requires_active_profile(self, stub):
        # AC6a adjacency: no active profile → 409, never a profiles-root write.
        r = stub.client.post("/api/setup/save", json={"ftp": 250})
        assert r.status_code == 409
        assert not (stub.home / ".domestique" / "profiles" / "athlete.json").exists()


class TestSetupSaveAtomicity:
    def test_invalid_payload_writes_nothing(self, stub):
        # AC4b validate-all-then-write: an lthr>=max_hr 400 must leave NO
        # partial state — no creds, no athlete change, no prefs, no marker.
        pid = _active_profile(stub)
        prof_dir = stub.home / ".domestique" / "profiles" / pid
        before_athlete = (prof_dir / "athlete.json").read_text(encoding="utf-8")
        before_env = (prof_dir / ".env").read_text(encoding="utf-8") \
            if (prof_dir / ".env").exists() else None
        before_prefs = (prof_dir / "user_prefs.json").read_text(encoding="utf-8") \
            if (prof_dir / "user_prefs.json").exists() else None

        r = stub.client.post("/api/setup/save", json={
            "icu_athlete_id": "i77", "icu_api_key": "key77",
            "lthr": 190, "max_hr": 170,
            "hours_per_week": 12, "available_days": [0, 1], "rest_days": [6],
        })
        assert r.status_code == 400
        assert "LTHR" in r.json()["detail"]

        assert (prof_dir / "athlete.json").read_text(encoding="utf-8") == before_athlete
        after_env = (prof_dir / ".env").read_text(encoding="utf-8") \
            if (prof_dir / ".env").exists() else None
        assert after_env == before_env            # creds NOT written on 400
        after_prefs = (prof_dir / "user_prefs.json").read_text(encoding="utf-8") \
            if (prof_dir / "user_prefs.json").exists() else None
        assert after_prefs == before_prefs
        assert not app_module._setup_marker().exists()

    def test_untouched_defaults_write_nothing(self, stub):
        # A5: the wizards OMIT untouched fields; an empty save writes no
        # athlete data, no provenance, no FTP-test ledger rows — just the
        # completion marker.
        pid = _active_profile(stub)
        prof_dir = stub.home / ".domestique" / "profiles" / pid
        before = (prof_dir / "athlete.json").read_text(encoding="utf-8")
        r = stub.client.post("/api/setup/save", json={})
        assert r.status_code == 200, r.text
        after = (prof_dir / "athlete.json").read_text(encoding="utf-8")
        assert after == before
        assert "lthr_source" not in after
        assert "ftp_test_history" not in after
        assert app_module._setup_marker().exists()

    def test_day_grid_overlap_rejected(self, stub):
        # AC6b: a day can't be both a training day and a rest day.
        _active_profile(stub)
        r = stub.client.post("/api/setup/save", json={
            "available_days": [0, 1, 2], "rest_days": [2, 6]})
        assert r.status_code == 400
        assert "rest day" in r.json()["detail"]

    def test_hours_out_of_range_rejected(self, stub):
        _active_profile(stub)
        r = stub.client.post("/api/setup/save", json={"hours_per_week": 55})
        assert r.status_code == 400
        assert "hours_per_week" in r.json()["detail"]


# ---------------------------------------------------------------------------
# A5 — wizard invariants
# ---------------------------------------------------------------------------

class TestWizardInvariants:
    def test_lthr_ge_maxhr_400_with_detail(self, stub):
        _active_profile(stub)
        r = stub.client.post("/api/setup/save",
                             json={"lthr": 180, "max_hr": 170})
        assert r.status_code == 400
        assert "LTHR" in r.json()["detail"] and "170" in r.json()["detail"]

    def test_extras_persist(self, stub):
        pid = _active_profile(stub)
        r = stub.client.post("/api/setup/save", json={
            "ftp": 255, "weight": 70,
            "age": 41, "sex": "F", "cp": 260, "wprime_j": 21000})
        assert r.status_code == 200, r.text
        athlete = _athlete_on_disk(stub, pid)
        assert athlete.get("age") == 41
        assert athlete.get("sex") == "F"
        assert athlete.get("cp") == 260
        assert athlete.get("wprime_j") == 21000
        assert athlete.get("ftp") == 255

    def test_target_mode_hr_requires_lthr(self, stub):
        pid = _active_profile(stub)
        # Virgin profile must NOT have a seeded lthr (CORE AC5e); make the
        # test self-sufficient either way.
        if "lthr" in stub.pm._athlete:
            stub.pm._athlete.pop("lthr", None)
            stub.pm._write_json(stub.pm.active_dir / "athlete.json", stub.pm._athlete)
        r = stub.client.post("/api/setup/save", json={"target_mode": "hr"})
        assert r.status_code == 400
        assert "LTHR" in r.json()["detail"]
        # With a typed LTHR the mode lands, stamped manual (user typed it).
        r = stub.client.post("/api/setup/save", json={
            "target_mode": "hr", "lthr": 162, "max_hr": 195})
        assert r.status_code == 200, r.text
        athlete = _athlete_on_disk(stub, pid)
        assert athlete.get("target_mode") == "hr"
        assert athlete.get("lthr") == 162
        assert athlete.get("lthr_source") == "manual"

    def test_icu_prefill_hint_stamps_icu_source(self, stub):
        # AC5f: an ICU-prefilled, unedited LTHR stamps source=icu (not manual).
        pid = _active_profile(stub)
        r = stub.client.post("/api/setup/save", json={
            "lthr": 158, "max_hr": 190, "lthr_source_hint": "icu"})
        assert r.status_code == 200, r.text
        assert _athlete_on_disk(stub, pid).get("lthr_source") == "icu"

    def test_limits_endpoint_is_the_one_table(self, stub):
        lim = stub.client.get("/api/setup/limits").json()
        assert lim == {k: list(map(float, v)) if isinstance(v, list) else v
                       for k, v in app_module.SETUP_LIMITS.items()} or \
               lim == app_module.SETUP_LIMITS
        # Locked contract ranges.
        assert lim["max_hr"] == [140, 220]
        assert lim["weight"] == [30, 200]
        assert lim["lthr"] == [100, 220]
        assert lim["hours_per_week"] == [1, 40]

    def test_update_settings_uses_same_table(self, stub):
        _active_profile(stub)
        r = stub.client.post("/api/settings", json={"lthr": 230})
        assert r.status_code == 400
        assert "220" in r.json()["detail"]


# ---------------------------------------------------------------------------
# A9 — OAuth binding: two profiles, either order; deleted profile
# ---------------------------------------------------------------------------

class TestOauthBinding:
    @pytest.mark.parametrize("order", ["active_first", "inactive_first"])
    def test_two_flows_bind_to_their_own_profiles(self, stub, order):
        a = stub.pm.create_profile("Alice")
        b = stub.pm.create_profile("Bob")
        stub.pm.switch(a)
        db_module._sync_stop.clear()
        app_module._icu_oauth_states["SA"] = {"profile_id": a, "ts": 9e12}
        app_module._icu_oauth_states["SB"] = {"profile_id": b, "ts": 9e12}

        calls = [("CA", "SA"), ("CB", "SB")]
        if order == "inactive_first":
            calls.reverse()
        with mock.patch("httpx.post", side_effect=_fake_exchange()):
            for code, state in calls:
                r = stub.client.get(f"/oauth/icu/callback?code={code}&state={state}",
                                    follow_redirects=False)
                assert "icu=connected" in r.headers["location"], r.headers["location"]

        env_a = _env_on_disk(stub, a)
        env_b = _env_on_disk(stub, b)
        assert env_a.get("ICU_ACCESS_TOKEN") == "TOK_CA"
        assert env_a.get("ICU_ATHLETE_ID") == "i-CA"
        assert env_b.get("ICU_ACCESS_TOKEN") == "TOK_CB"
        assert env_b.get("ICU_ATHLETE_ID") == "i-CB"
        # The ACTIVE profile's in-memory creds are Alice's — Bob's flow never
        # leaked into the active identity.
        assert stub.pm.active_id == a
        assert stub.pm.icu_athlete_id == "i-CA"

    def test_refresh_token_captured_when_present(self, stub):
        # AC3c (capture only): when ICU's token response carries a refresh
        # token, it lands in the target profile's .env — non-active path too.
        a = stub.pm.create_profile("Alice")
        b = stub.pm.create_profile("Bob")
        stub.pm.switch(a)
        db_module._sync_stop.clear()
        app_module._icu_oauth_states["SR"] = {"profile_id": b, "ts": 9e12}

        class _RespWithRefresh(_TokenResp):
            def json(self):
                d = super().json()
                d["refresh_token"] = "REFRESH_XYZ"
                d["expires_in"] = 3600
                return d

        with mock.patch("httpx.post",
                        side_effect=lambda url, data=None, **kw:
                        _RespWithRefresh((data or {}).get("code", "?"))):
            r = stub.client.get("/oauth/icu/callback?code=CR&state=SR",
                                follow_redirects=False)
        assert "icu=connected" in r.headers["location"]
        env_b = _env_on_disk(stub, b)
        assert env_b.get("ICU_REFRESH_TOKEN") == "REFRESH_XYZ"
        assert env_b.get("ICU_TOKEN_EXPIRES_AT", "").isdigit()

    def test_deleted_profile_callback_never_binds(self, stub):
        a = stub.pm.create_profile("Alice")
        stub.pm.switch(a)
        db_module._sync_stop.clear()
        app_module._icu_oauth_states["SG"] = {"profile_id": "ghost", "ts": 9e12}
        with mock.patch("httpx.post", side_effect=_fake_exchange()):
            r = stub.client.get("/oauth/icu/callback?code=CG&state=SG",
                                follow_redirects=False)
        assert "reason=profile_gone" in r.headers["location"]
        assert _env_on_disk(stub, a).get("ICU_ACCESS_TOKEN", "") == ""
        assert not (stub.home / ".domestique" / "profiles" / "ghost").exists()


# ---------------------------------------------------------------------------
# A4 — disconnect / different-athlete purge
# ---------------------------------------------------------------------------

def _seed_synced_data(stub, pid):
    """Old-athlete residue: DB rows + an icu-sourced athlete mirror."""
    db_module.set_db_path(stub.pm.db_path)
    db_module.close_all_connections()
    db_module.init_db()
    conn = db_module.get_db()
    conn.execute("INSERT INTO activities (id, date) VALUES ('a1', '2026-06-01')")
    conn.execute("INSERT INTO wellness (date, ctl, atl) VALUES ('2026-06-01', 50, 40)")
    conn.execute("INSERT INTO sync_log (timestamp) VALUES ('2026-06-01T10:00:00')")
    conn.commit()
    stub.pm._athlete.update({"lthr": 155, "lthr_source": "icu"})
    stub.pm._write_json(stub.pm.active_dir / "athlete.json", stub.pm._athlete)


def _counts(stub, pid):
    import sqlite3
    p = stub.home / ".domestique" / "profiles" / pid / "health_tracker.db"
    c = sqlite3.connect(str(p))
    try:
        return tuple(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in ("activities", "wellness", "sync_log"))
    finally:
        c.close()


class TestPurge:
    def test_reconnect_different_athlete_purges(self, stub):
        pid = _active_profile(stub, "Alice")
        stub.pm.save_icu_token("T_OLD", "i_old", "Old Rider")
        _seed_synced_data(stub, pid)
        assert _counts(stub, pid) == (1, 1, 1)

        app_module._icu_oauth_states["SN"] = {"profile_id": pid, "ts": 9e12}
        with mock.patch("httpx.post", side_effect=_fake_exchange()):
            r = stub.client.get("/oauth/icu/callback?code=NEW&state=SN",
                                follow_redirects=False)
        assert "icu=connected" in r.headers["location"]
        assert _counts(stub, pid) == (0, 0, 0)          # old rows purged
        athlete = _athlete_on_disk(stub, pid)
        assert "lthr" not in athlete                     # icu mirror reset
        assert "lthr_source" not in athlete
        env = _env_on_disk(stub, pid)
        assert env.get("ICU_ACCESS_TOKEN") == "TOK_NEW"
        assert env.get("ICU_ATHLETE_ID") == "i-NEW"

    def test_same_athlete_reconnect_does_not_purge(self, stub):
        pid = _active_profile(stub, "Alice")
        stub.pm.save_icu_token("T_OLD", "i-SAME", "Rider")
        _seed_synced_data(stub, pid)
        app_module._icu_oauth_states["SS"] = {"profile_id": pid, "ts": 9e12}
        with mock.patch("httpx.post", side_effect=_fake_exchange()):
            r = stub.client.get("/oauth/icu/callback?code=SAME&state=SS",
                                follow_redirects=False)
        assert "icu=connected" in r.headers["location"]
        assert _counts(stub, pid) == (1, 1, 1)          # same athlete → kept

    def test_disconnect_purges_and_clears_token(self, stub):
        pid = _active_profile(stub, "Alice")
        stub.pm.save_icu_token("T_OLD", "i_old", "Old Rider")
        _seed_synced_data(stub, pid)
        r = stub.client.post("/api/icu/disconnect")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert _counts(stub, pid) == (0, 0, 0)
        env = _env_on_disk(stub, pid)
        assert env.get("ICU_ACCESS_TOKEN", "") == ""
        athlete = _athlete_on_disk(stub, pid)
        assert "lthr" not in athlete


# ---------------------------------------------------------------------------
# A13 / AC1 scope-add — app-side write-gate wiring
# ---------------------------------------------------------------------------

class TestSyncWriteGateWiring:
    def test_snapshot_is_gate_compatible_and_aborts_after_switch(self, stub):
        a = stub.pm.create_profile("A")
        b = stub.pm.create_profile("B")
        stub.pm.switch(a)
        db_module._sync_stop.clear()
        snap = app_module._sync_task_snapshot()
        assert len(snap) == 3           # the exact db.snapshot_sync_identity tuple
        with db_module.sync_write_gate(snap):
            pass                        # same identity → writes allowed
        stub.pm.switch(b)
        db_module._sync_stop.clear()
        with pytest.raises(db_module.SyncAborted):
            with db_module.sync_write_gate(snap):
                pass                    # stale snapshot → writes refused

    def test_switch_endpoint_maps_syncbusy_to_503(self, stub):
        a = stub.pm.create_profile("A")
        with mock.patch.object(stub.pm, "switch",
                               side_effect=db_module.SyncBusy("wedged")):
            r = stub.client.post("/api/profiles/switch", json={"id": a})
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# Structural template assertions (banner, per-profile keys, wizard wiring)
# ---------------------------------------------------------------------------

def _tpl(name):
    return (REPO / "templates" / name).read_text(encoding="utf-8")


def _input_minmax(html, input_id):
    m = re.search(r'<input[^>]*id="%s"[^>]*>' % re.escape(input_id), html)
    assert m, f"input #{input_id} not found"
    tag = m.group(0)
    lo = re.search(r'min="([\d.]+)"', tag)
    hi = re.search(r'max="([\d.]+)"', tag)
    assert lo and hi, f"input #{input_id} lacks min/max: {tag}"
    return float(lo.group(1)), float(hi.group(1))


class TestTemplatesStructural:
    def test_dashboard_banner_links_to_connection_card(self):
        html = _tpl("dashboard.html")
        assert "openSettingsConnections" in html
        assert "Reconnect intervals.icu" in html
        assert 'id="icu-connection-card"' in html
        # The paused banner no longer points at the first-run wizard.
        banner = html[html.index("Intervals.icu sync paused"):][:400]
        assert 'href="/setup"' not in banner

    def test_dashboard_no_athlete_id_surface(self):
        html = _tpl("dashboard.html")
        assert "no_athlete_id" in html
        assert 'id="icu-conn-error"' in html

    def test_dashboard_switch_account_is_disconnect_then_start(self):
        html = _tpl("dashboard.html")
        i = html.index("async function icuSwitchAccount")
        body = html[i:i + 900]
        assert "/api/icu/disconnect" in body
        assert "/oauth/icu/start" in body
        assert body.index("/api/icu/disconnect") < body.index("/oauth/icu/start")

    def test_dashboard_per_profile_local_storage_keys(self):
        html = _tpl("dashboard.html")
        assert "{{ active_profile_id }}" in html
        assert "_profileLsKey('domestiqueVolUnit')" in html
        assert "_profileLsKey('dfa_backfill_last_run_at')" in html
        assert "_profileLsKey('dfa_migrate_last_run_at')" in html

    def test_setup_wizard_is_oauth_only(self):
        html = _tpl("setup.html")
        assert "icu_api_key" not in html            # creds never in the save payload
        assert "setSource" not in html              # per-field API-key toggles gone
        assert "use_connection" in html             # Garmin check via saved connection
        assert "/api/icu/athlete-numbers" in html   # same auto-fill path as profile_setup
        assert "lthr_source_hint" in html           # AC5f
        assert "target_mode" in html                # AC5e question
        assert "/api/setup/limits" in html          # AC5d one table

    def test_profile_setup_defers_create_to_finish(self):
        html = _tpl("profile_setup.html")
        assert html.count("/api/profiles/create") == 1
        assert html.index("function doFinish") < html.index("/api/profiles/create")
        assert "swData.active !== createdId" in html   # switch response checked
        assert "/api/setup/limits" in html
        assert "target_mode" in html

    def test_client_ranges_match_server_table(self):
        lim = app_module.SETUP_LIMITS
        setup = _tpl("setup.html")
        for field, iid in [("weight", "s-weight"), ("ftp", "s-ftp"),
                           ("lthr", "s-lthr"), ("max_hr", "s-maxhr"),
                           ("hours_per_week", "s-hours"), ("age", "s-age"),
                           ("cp", "s-cp"), ("wprime_j", "s-wprime")]:
            assert _input_minmax(setup, iid) == (lim[field][0], lim[field][1]), \
                f"setup.html #{iid} range drifted from SETUP_LIMITS[{field}]"
        ps = _tpl("profile_setup.html")
        for field, iid in [("ftp", "ftp"), ("weight", "weight"),
                           ("lthr", "lthr"), ("max_hr", "max-hr"),
                           ("age", "age"), ("cp", "cp"), ("wprime_j", "wprime")]:
            assert _input_minmax(ps, iid) == (lim[field][0], lim[field][1]), \
                f"profile_setup.html #{iid} range drifted from SETUP_LIMITS[{field}]"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
