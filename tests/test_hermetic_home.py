"""3.4.3 — hermetic-fs gate: the suite must NEVER write the user's real home.

Owner incident (2026-07-16): a release-gate run replaced the owner's live
~/.domestique/profiles/<name>/plans/current_plan.json with a fixture plan
(goal.event_name == "v134-test", from test_v134_generate_plan_no_yellow's
TestClient POST to /api/plan/generate). Every app data path resolves through
Path.home()/.domestique at import time, and profile_manager rebinds
training_planner.PLAN_DIR to the ACTIVE PROFILE's plan dir — so any
unsandboxed test that generates/updates a plan stomped the real store.

conftest.py now swaps HOME to a per-process sandbox BEFORE any project
import (keeping the real path in DOMESTIQUE_REAL_HOME for read-only
validations). These tests pin that contract.
"""
import json
import os
from pathlib import Path

import pytest

import app as app_module
import training_planner as tp


def test_home_is_sandboxed():
    real = os.environ.get("DOMESTIQUE_REAL_HOME")
    assert real, "conftest must record the real home in DOMESTIQUE_REAL_HOME"
    assert str(Path.home()) != real, "HOME must be swapped to a sandbox"
    assert "domestique-test-home-" in str(Path.home()), (
        f"HOME does not look like the conftest sandbox: {Path.home()}")


def test_app_data_paths_resolve_inside_sandbox():
    real = os.environ.get("DOMESTIQUE_REAL_HOME") or ""
    sandbox = str(Path.home())
    # The two import-time roots every plan write funnels through.
    plan_dir = str(app_module._plan_dir())
    assert plan_dir.startswith(sandbox), (
        f"app._plan_dir() escapes the sandbox: {plan_dir}")
    assert not plan_dir.startswith(str(Path(real) / ".domestique")), (
        f"app._plan_dir() points at the REAL home: {plan_dir}")
    tp_dir = str(tp.PLAN_DIR)
    assert tp_dir.startswith(sandbox), (
        f"training_planner.PLAN_DIR escapes the sandbox: {tp_dir}")


def test_generate_endpoint_writes_only_the_sandbox():
    """Behavioral pin on the exact leak path: POST /api/plan/generate via
    TestClient and assert the plan landed in the SANDBOX plan dir (the write
    path itself proves no real-home write — _plan_dir() is sandboxed and is
    the only path the endpoint writes)."""
    from fastapi.testclient import TestClient

    sandbox = str(Path.home())
    with TestClient(app_module.app) as client:
        r = client.post("/api/plan/generate", json={
            "goal_type": "event",
            "event_name": "hermetic-fs-test",
            "event_km": 120, "event_climb": 800,
            "event_type": "gran fondo",
            "hours_per_week": 8, "plan_weeks": 8,
            "rest_days": [0], "available_days": [1, 2, 3, 4, 5, 6],
        })
        assert r.status_code == 200, r.text
    written = app_module._plan_dir() / "current_plan.json"
    assert str(written).startswith(sandbox)
    assert written.exists(), "generate must persist the plan in the sandbox"
    goal = (json.loads(written.read_text()).get("goal") or {})
    assert goal.get("event_name") == "hermetic-fs-test"


@pytest.mark.skipif(
    not os.environ.get("DOMESTIQUE_REAL_HOME"), reason="no real home recorded")
def test_real_home_helper_points_outside_sandbox():
    real = Path(os.environ["DOMESTIQUE_REAL_HOME"])
    assert str(Path.home()) != str(real)
    # Never a sandbox artifact: the recorded real home must not be a tmp dir
    # minted by this gate.
    assert "domestique-test-home-" not in str(real)
