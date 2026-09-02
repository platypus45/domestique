"""v3.11.3 multi-profile screen — two accounts on one app must never see
each other's FTP-test artifacts, plan choices, library dirs or diagnostics.

Mirrors tests/test_account_core.py's harness (stub HOME, fresh ProfileManager,
module-state guard) and drives the real endpoints this week touched.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import db as db_mod  # noqa: E402
import profile_manager as pm_mod  # noqa: E402
import ride_storage as rs  # noqa: E402
import training_planner as tp  # noqa: E402

HTML = (Path(__file__).resolve().parent.parent / "src" / "templates"
        / "dashboard.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _module_state_guard():
    saved_env = {k: os.environ.get(k) for k in
                 ("ICU_ATHLETE_ID", "ICU_API_KEY", "ICU_ACCESS_TOKEN")}
    orig_db_path = db_mod.DB_PATH
    orig_plan_dir = tp.PLAN_DIR
    orig_workout_dir = tp.WORKOUT_DIR
    yield
    db_mod.shutdown_sync()
    if db_mod._sync_write_lock.locked():
        try:
            db_mod._sync_write_lock.release()
        except RuntimeError:
            pass
    db_mod.close_all_connections()
    db_mod.set_db_path(orig_db_path)
    tp.PLAN_DIR = orig_plan_dir
    tp.WORKOUT_DIR = orig_workout_dir
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    pm_mod.ProfileManager._instance = None


def _mk_pm(home: Path):
    pm_mod.ProfileManager._instance = None
    with patch("pathlib.Path.home", return_value=Path(home)):
        pm = pm_mod.ProfileManager.get()
    return pm


def _two(tmp_path):
    pm = _mk_pm(tmp_path)
    a = pm.create_profile("Alice")
    b = pm.create_profile("Bob")
    return pm, a, b


def _client():
    import app as app_module
    from fastapi.testclient import TestClient
    return app_module, TestClient(app_module.app)


def _envelope_with_suggestion():
    import fitness_estimation as fe
    series = [120] * 600 + [300] * 300 + [120] * 540 + [280] * 1200 + [100] * 300
    out = fe.evaluate_ftp_test(series, prior_ftp=250)
    assert out
    return {"id": "777", "type": "Ride", "name": "FTP Test — Coggan 20min protocol (59min)",
            "start_date_local": "2026-09-02T09:00:00", "duration": len(series)}, out, series


# ── 1. suggestion + review verdict are invisible to the other profile ───────

def test_ftp_suggestion_and_review_stay_in_their_profile(tmp_path):
    pm, a, b = _two(tmp_path)
    app_module, client = _client()
    pm.switch(a); app_module._apply_profile_paths()
    payload, out, series = _envelope_with_suggestion()
    p = rs.persist_icu_activity(payload)
    env = json.loads(p.read_text()); env.update(out); env["streams"] = {"watts": series}
    p.write_text(json.dumps(env))
    r = client.post("/api/ride/icu_777/ftp-test-review", json={"action": "declined", "ftp": 266})
    assert r.status_code == 200 and r.json()["persisted"] is True
    assert client.get("/api/rides/icu_777").status_code == 200
    assert client.get("/api/ride/icu_777/ftp-test-analysis").status_code == 200

    pm.switch(b); app_module._apply_profile_paths()
    assert client.get("/api/rides/icu_777").status_code == 404
    assert client.get("/api/ride/icu_777/ftp-test-analysis").status_code == 404
    assert client.post("/api/ride/icu_777/ftp-test-review",
                       json={"action": "accepted", "ftp": 270}).status_code == 404
    assert rs.load_icu_rides() == []

    pm.switch(a); app_module._apply_profile_paths()
    d = client.get("/api/rides/icu_777").json()
    assert d["summary"]["ftp_test_review"]["action"] == "declined"   # untouched by B's attempt
    assert d["summary"]["ftp_test_suggestion"]["ftp"] == out["ftp_test_suggestion"]["ftp"]


# ── 2. the protocol choice lands in the active profile's plan only ──────────

def _seed_plan(app_module, day="2026-09-05"):
    plan = {"weeks": [{"week_num": 1, "start": "2026-08-31", "end": "2026-09-06",
                       "sessions": [{"day": day, "day_name": "Fri", "session_type": "ftp_test",
                                     "duration_min": 60, "tss_estimate": 72.0, "description": "",
                                     "zwo_file": "ftp_test_coggan_3x1min-1min_95pct_59min.zwo",
                                     "zwo_name": "FTP Test", "status": "pending"}]}]}
    d = app_module._plan_dir(); d.mkdir(parents=True, exist_ok=True)
    (d / "current_plan.json").write_text(json.dumps(plan))
    return d / "current_plan.json"


def test_protocol_choice_is_per_profile(tmp_path):
    pm, a, b = _two(tmp_path)
    app_module, client = _client()
    pm.switch(a); app_module._apply_profile_paths()
    plan_a = _seed_plan(app_module)
    r = client.post("/api/plan/ftp-test-type", json={"date": "2026-09-05", "test_type": "ramp"})
    assert r.status_code == 200, r.text
    assert json.loads(plan_a.read_text())["weeks"][0]["sessions"][0]["ftp_test_type"] == "ramp"

    pm.switch(b); app_module._apply_profile_paths()
    assert app_module._plan_dir() != plan_a.parent
    r = client.post("/api/plan/ftp-test-type", json={"date": "2026-09-05", "test_type": "sixty_min"})
    assert r.status_code in (400, 404)          # B has no plan — must not touch A's
    assert json.loads(plan_a.read_text())["weeks"][0]["sessions"][0]["ftp_test_type"] == "ramp"


# ── 3. a custom workout dir on A never leaks into B; diagnostics follow ────

def test_custom_workout_dir_and_diagnostics_follow_the_switch(tmp_path):
    pm, a, b = _two(tmp_path)
    app_module, client = _client()
    custom = tmp_path / "alice_lib"; custom.mkdir()
    (custom / "endurance_steady_60min.zwo").write_text(
        (Path(tp._BUNDLED_WORKOUT_DIR) / "ftp_test_coggan_3x1min-1min_95pct_59min.zwo").read_text())
    # The override must exist BEFORE activation — switch() is a no-op for the
    # already-active profile (by design), so write it first, then switch in.
    (pm._profiles_dir / a / "user_paths.json").write_text(json.dumps({"workout_dir": str(custom)}))
    pm.switch(a); app_module._apply_profile_paths()
    assert Path(tp.WORKOUT_DIR) == custom
    app_module._DIAG_HEALTH_CACHE["result"] = None
    ha = client.get("/api/diag/health").json()["checks"]["pool_health"]
    assert ha["workout_dir_is_bundled"] is False and ha["workout_dir"] == str(custom)

    pm.switch(b); app_module._apply_profile_paths()
    assert Path(tp.WORKOUT_DIR) == Path(tp._BUNDLED_WORKOUT_DIR)
    # No manual cache reset: the switch hook must have dropped A's snapshot.
    hb = client.get("/api/diag/health").json()["checks"]["pool_health"]
    assert hb["workout_dir_is_bundled"] is True
    assert hb["workout_dir"] != str(custom)


# ── 4. per-tab "already shown" flag is scoped by profile ────────────────────

def test_modal_seen_flag_is_profile_scoped():
    assert "'ftp-modal-seen-' + __ACTIVE_PROFILE_ID + ':' + rideId" in HTML
    assert "'ftp-modal-seen-' + rideId" not in HTML
