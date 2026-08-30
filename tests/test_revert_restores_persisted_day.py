"""'Ride the original anyway' restores the plan, not just a flag.

The rider's report: HRV tanked, the day was downgraded, and the revert button
did nothing visible — the preview closed, the replaced workout stayed, and
only after several open/click cycles did the original come back.

Mechanism: the daily adjustment is PERSISTED into current_plan.json
(api_today_session_persist overwrites the session in place), while
/api/readiness/revert-cap only wrote a one-day suppression flag. The live
recompute honoured the flag; every view reading the plan kept showing the
stored replacement. Worse, the persist clobbered the original prescription,
so there was nothing to restore FROM.

Pinned here: (1) persisting an adaptation stashes the original once —
a second adaptation of the same day must not re-stash the replacement over
it; (2) revert-cap restores the stashed original and clears the adapted
marks; (3) a day adapted before this fix (no stash) reverts the flag without
touching the plan — never invent a restoration.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture()
def client_with_plan(tmp_path, monkeypatch):
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    weeks = [{
        "week_num": 1, "start": monday.isoformat(),
        "end": (monday + timedelta(days=6)).isoformat(),
        "phase": "build1", "tss_target": 300,
        "sessions": [
            {"day": (monday + timedelta(days=i)).isoformat(),
             "day_name": (monday + timedelta(days=i)).strftime("%A"),
             "description": "Threshold 4x8", "session_type": "threshold",
             "duration_min": 60, "tss_estimate": 68, "status": "pending",
             "zwo_file": "threshold_4x8min-4min_98pct_62min.zwo",
             "zwo_name": "Threshold 4x8"}
            for i in range(7)
        ],
    }]
    d = tmp_path / "plans"; d.mkdir(parents=True)
    (d / "current_plan.json").write_text(json.dumps(
        {"goal": {}, "phases": [], "weeks": weeks}), encoding="utf-8")
    monkeypatch.setattr(app_module, "_plan_dir", lambda: d)
    # Point the one-day flag at the sandbox too, or a flag left by an earlier
    # test (or a real profile) leaks in.
    flag = tmp_path / "readiness_cap_reverted.txt"
    monkeypatch.setattr(app_module, "_readiness_revert_flag_path", lambda: flag)
    return TestClient(app_module.app), d / "current_plan.json"


def _today_session(path):
    plan = json.loads(path.read_text())
    t = date.today().isoformat()
    return next(s for w in plan["weeks"] for s in w["sessions"]
                if s["day"] == t)


def test_persist_stashes_the_original_once(client_with_plan):
    client, path = client_with_plan
    r = client.post("/api/today-session/persist", json={
        "reason": "HRV below baseline", "session_type": "z2",
        "duration_min": 60, "tss_estimate": 45})
    assert r.status_code == 200, r.text
    s = _today_session(path)
    assert s["session_type"] == "z2" and s["adapted"] is True
    assert s["pre_adapt"]["session_type"] == "threshold"
    assert s["pre_adapt"]["zwo_file"] == "threshold_4x8min-4min_98pct_62min.zwo"

    # A second adaptation (readiness worsened further) must keep the FIRST
    # stash — re-stashing would save z2 as "the original".
    r = client.post("/api/today-session/persist", json={
        "reason": "soreness", "session_type": "recovery"})
    assert r.status_code == 200
    assert _today_session(path)["pre_adapt"]["session_type"] == "threshold"


def test_revert_restores_the_stashed_original(client_with_plan):
    client, path = client_with_plan
    client.post("/api/today-session/persist", json={
        "reason": "HRV below baseline", "session_type": "z2",
        "duration_min": 60, "tss_estimate": 45})
    r = client.post("/api/readiness/revert-cap", json={"signal": "any"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored"], (
        "revert set the flag but left the persisted replacement in the plan — "
        "this is exactly the click-does-nothing report")
    assert body["restored"]["session_type"] == "threshold"
    s = _today_session(path)
    assert s["session_type"] == "threshold"
    assert s["zwo_file"] == "threshold_4x8min-4min_98pct_62min.zwo"
    assert not s.get("adapted")
    assert "pre_adapt" not in s and "adapted_reason" not in s


def test_revert_without_a_stash_touches_nothing(client_with_plan):
    """A day adapted under the old code has no stash. Restoring by guesswork
    would be worse than the bug; the flag alone must suffice there."""
    client, path = client_with_plan
    plan = json.loads(path.read_text())
    t = date.today().isoformat()
    for w in plan["weeks"]:
        for s in w["sessions"]:
            if s["day"] == t:
                s["session_type"] = "z2"
                s["adapted"] = True
                s["adapted_reason"] = "legacy adapt, pre-stash"
    path.write_text(json.dumps(plan), encoding="utf-8")

    r = client.post("/api/readiness/revert-cap", json={"signal": "any"})
    assert r.status_code == 200
    assert r.json()["restored"] is None
    s = _today_session(path)
    assert s["session_type"] == "z2" and s["adapted"] is True
