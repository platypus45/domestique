"""v1.7.2 — when availability cap shrinks a session, re-match the ZWO.

User report (screenshot): "Wednesday — Endurance 6x2min (45min)" in the
modal title but the chart rendered 90 minutes worth of segments. Cause:
v1.7.1 shrank ``session.duration_min`` (110 → 45) and recomputed TSS, but
left ``session.zwo_file`` / ``zwo_name`` pointing at the original
90-minute library file. The dashboard's openWorkoutDetail() fetched THAT
file's segments → 90 min of bars in the chart.

v1.7.2 re-runs ``match_zwo`` on any cap that shrinks duration by ≥ 15 %
so the loaded ZWO actually fits the new duration. The previous ZWO name
is added to ``used_names`` to force a different pick; if the library
has nothing short enough we clear ``zwo_file`` / ``zwo_name`` instead
of leaving the wrong workout behind.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


def _isolate_plan_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(app_module, "_plan_dir", lambda: tmp_path)
    return tmp_path


def _seed_plan_with_long_session(plan_dir: Path) -> tuple[Path, str]:
    json_path = plan_dir / "current_plan.json"
    today = date.today()
    monday = today + timedelta(days=(7 - today.weekday()))
    wed = (monday + timedelta(days=2)).isoformat()
    plan = {
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0]},
        "phases": [],
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "sessions": [{
                "day": wed,
                "day_name": "Wed",
                "session_type": "z2",
                "duration_min": 110,
                "tss_estimate": 82.0,
                "description": "Z2 endurance",
                "zwo_file": "z2_endurance_110min.zwo",
                "zwo_name": "Endurance Long (110min)",
                "status": "pending",
            }],
        }],
        "availability": {},
    }
    json_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return json_path, wed


def test_cap_triggers_match_zwo_rematch(tmp_path, monkeypatch):
    """A meaningful shrink (110 → 45 = 59 % drop) must invoke match_zwo
    so the ZWO swaps to one that fits the new duration."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan_with_long_session(plan_dir)

    real_match_zwo = tp.match_zwo
    called: list[dict] = []

    def _spy(session, library, **kwargs):
        called.append({
            "duration_min": session.duration_min,
            "used_names": set(kwargs.get("used_names") or ()),
        })
        # Simulate a successful match by stamping a new ZWO on the session.
        session.zwo_file = f"replacement_{session.duration_min}min.zwo"
        session.zwo_name = f"Replacement ({session.duration_min}min)"
        return session

    monkeypatch.setattr(tp, "match_zwo", _spy)
    # Speed: stub load_workout_library so the test doesn't read 3k+ ZWO files.
    monkeypatch.setattr(tp, "load_workout_library", lambda: [{"Name": "x", "File": "x.zwo", "Duration(min)": 45}])

    client = TestClient(app_module.app)
    plan_path = plan_dir / "current_plan.json"
    plan_before = json.loads(plan_path.read_text(encoding="utf-8"))
    wed = plan_before["weeks"][0]["sessions"][0]["day"]

    r = client.post(
        "/api/plan/save-availability",
        json={"availability": {wed: {"hours": 0.75, "type": "available"}}},
    )
    assert r.status_code == 200, r.text

    assert called, "match_zwo was not invoked when cap shrank the session"
    # The re-match call must see the SHRUNK duration_min (45), not 110.
    assert called[0]["duration_min"] == 45, \
        f"match_zwo saw duration_min={called[0]['duration_min']} (expected 45)"
    # used_names must include the original ZWO so match_zwo doesn't
    # hand us back the same workout.
    assert "Endurance Long (110min)" in called[0]["used_names"], \
        f"original ZWO not excluded: {called[0]['used_names']}"

    persisted = json.loads(plan_path.read_text(encoding="utf-8"))
    sess = persisted["weeks"][0]["sessions"][0]
    assert sess["duration_min"] == 45
    assert sess["zwo_file"] == "replacement_45min.zwo"
    assert sess["zwo_name"] == "Replacement (45min)"


def test_cap_skips_rematch_for_trivial_shrink(tmp_path, monkeypatch):
    """A 110 → 100 cap is only 9 % — not worth re-matching. The original
    ZWO stays bound; the duration / TSS still update."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan_with_long_session(plan_dir)

    called: list = []
    monkeypatch.setattr(tp, "match_zwo", lambda *a, **kw: called.append(1) or a[0])
    monkeypatch.setattr(tp, "load_workout_library", lambda: [])

    client = TestClient(app_module.app)
    plan_path = plan_dir / "current_plan.json"
    plan_before = json.loads(plan_path.read_text(encoding="utf-8"))
    wed = plan_before["weeks"][0]["sessions"][0]["day"]

    r = client.post(
        "/api/plan/save-availability",
        json={"availability": {wed: {"hours": 100/60, "type": "available"}}},
    )
    assert r.status_code == 200

    assert not called, "match_zwo was invoked for a 9% shrink (should skip)"
    sess = json.loads(plan_path.read_text(encoding="utf-8"))["weeks"][0]["sessions"][0]
    assert sess["duration_min"] == 100  # cap still applied
    assert sess["zwo_name"] == "Endurance Long (110min)"  # ZWO unchanged


def test_cap_clears_zwo_when_no_candidate(tmp_path, monkeypatch):
    """If match_zwo raises NoCandidateWorkoutError (library has nothing
    short enough), v1.7.2 clears zwo_file/zwo_name so the UI flags the
    unmatched state instead of rendering the wrong workout's chart."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan_with_long_session(plan_dir)

    def _raise(session, library, **kwargs):
        raise tp.NoCandidateWorkoutError("empty_test")

    monkeypatch.setattr(tp, "match_zwo", _raise)
    monkeypatch.setattr(tp, "load_workout_library", lambda: [])

    client = TestClient(app_module.app)
    plan_path = plan_dir / "current_plan.json"
    plan_before = json.loads(plan_path.read_text(encoding="utf-8"))
    wed = plan_before["weeks"][0]["sessions"][0]["day"]

    r = client.post(
        "/api/plan/save-availability",
        json={"availability": {wed: {"hours": 0.5, "type": "available"}}},
    )
    assert r.status_code == 200

    sess = json.loads(plan_path.read_text(encoding="utf-8"))["weeks"][0]["sessions"][0]
    assert sess["duration_min"] == 30
    assert sess["zwo_file"] == ""
    assert sess["zwo_name"] == ""


def test_cap_real_library_picks_shorter_zwo(tmp_path, monkeypatch):
    """End-to-end with the real library: a 110 → 45 cap must land on a
    ZWO whose actual file duration is in the ~45-min ballpark, not the
    original 110-min file."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan_with_long_session(plan_dir)

    client = TestClient(app_module.app)
    plan_path = plan_dir / "current_plan.json"
    plan_before = json.loads(plan_path.read_text(encoding="utf-8"))
    wed = plan_before["weeks"][0]["sessions"][0]["day"]

    r = client.post(
        "/api/plan/save-availability",
        json={"availability": {wed: {"hours": 0.75, "type": "available"}}},
    )
    assert r.status_code == 200

    sess = json.loads(plan_path.read_text(encoding="utf-8"))["weeks"][0]["sessions"][0]
    assert sess["duration_min"] == 45
    assert sess["zwo_name"] != "Endurance Long (110min)", \
        "ZWO was NOT re-matched on a 59% shrink — chart will show 110-min content for a 45-min session"
