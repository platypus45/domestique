"""v1.7.5 — readiness "Apply tier-down" button.

User screenshot showed READINESS TODAY 4.7/10 with advice
"Soft tier-down recommended. Drop today's hard session by one tier."
Pre-v1.7.5 this was copy-only; user had to open the Plan tab and
manually use Rematch to act on it.

v1.7.5 wires the recommendation to one click:
``POST /api/readiness/apply-tier-down`` → server walks one step down
``tp._INTENSITY_LADDER`` via ``_drop_intensity``, recomputes TSS from
``TSS_PER_HOUR[new_type]``, re-matches the ZWO so the loaded workout
fits the new bucket, then runs a full ``tp.reforecast_dict`` so
downstream sessions catch up.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


def _isolate_plan_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(app_module, "_plan_dir", lambda: tmp_path)
    return tmp_path


def _seed_plan(plan_dir: Path, session_type: str = "vo2max",
               duration_min: int = 60, tss: float = 95.0) -> tuple[Path, str]:
    json_path = plan_dir / "current_plan.json"
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    today_iso = today.isoformat()
    plan = {
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0]},
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "sessions": [{
                "day": today_iso,
                "day_name": "Today",
                "session_type": session_type,
                "duration_min": duration_min,
                "tss_estimate": tss,
                "description": f"{session_type} session",
                "zwo_file": "original.zwo",
                "zwo_name": "Original Workout",
                "status": "pending",
            }],
        }],
        "availability": {},
    }
    json_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return json_path, today_iso


def _load_session(json_path: Path, day_iso: str) -> dict:
    plan = json.loads(json_path.read_text(encoding="utf-8"))
    for w in plan.get("weeks", []):
        for s in w.get("sessions", []):
            if s.get("day") == day_iso:
                return s
    return {}


def test_apply_tier_down_drops_one_step(tmp_path, monkeypatch):
    """vo2max → threshold (first step down the Seiler ladder)."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path, today_iso = _seed_plan(plan_dir, session_type="vo2max")

    client = TestClient(app_module.app)
    r = client.post("/api/readiness/apply-tier-down", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("old_type") == "vo2max"
    assert body.get("new_type") == "threshold"
    assert body.get("day") == today_iso

    sess = _load_session(json_path, today_iso)
    assert sess["session_type"] == "threshold"
    assert sess["adapted"] is True
    assert "Tier-down" in sess["adapted_reason"]


def test_apply_tier_down_recomputes_tss(tmp_path, monkeypatch):
    """TSS recomputes from TSS_PER_HOUR[new_type] × hours.

    vo2max at 60min planned 95 TSS → threshold at 60min should yield
    ~85 (TSS_PER_HOUR[threshold] = 85 → 60/60 × 85 = 85)."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path, today_iso = _seed_plan(plan_dir, session_type="vo2max",
                                       duration_min=60, tss=95.0)

    client = TestClient(app_module.app)
    r = client.post("/api/readiness/apply-tier-down", json={})
    assert r.status_code == 200
    body = r.json()

    expected_tss = round(60 / 60 * tp.TSS_PER_HOUR.get("threshold", 45), 1)
    assert body["new_tss"] == expected_tss

    sess = _load_session(json_path, today_iso)
    assert sess["tss_estimate"] == expected_tss


def test_apply_tier_down_rematches_zwo(tmp_path, monkeypatch):
    """A successful tier-down must swap the ZWO so the loaded workout
    fits the new session_type bucket. The original ZWO name is added
    to ``used_names`` so match_zwo can't return the same workout."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path, today_iso = _seed_plan(plan_dir, session_type="vo2max")

    captured: dict = {}

    def _spy(session, library, **kwargs):
        captured["session_type"] = session.session_type
        captured["used_names"] = set(kwargs.get("used_names") or ())
        session.zwo_file = "replacement.zwo"
        session.zwo_name = "Replacement Threshold"
        return session

    monkeypatch.setattr(tp, "match_zwo", _spy)
    monkeypatch.setattr(tp, "load_workout_library", lambda: [])

    client = TestClient(app_module.app)
    r = client.post("/api/readiness/apply-tier-down", json={})
    assert r.status_code == 200
    assert captured.get("session_type") == "threshold"
    assert "Original Workout" in captured.get("used_names", set())

    sess = _load_session(json_path, today_iso)
    assert sess["zwo_file"] == "replacement.zwo"
    assert sess["zwo_name"] == "Replacement Threshold"


def test_apply_tier_down_rest_day_short_circuits(tmp_path, monkeypatch):
    """Rest / recovery days are already at the bottom — no tier-down to apply."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan(plan_dir, session_type="rest", duration_min=0, tss=0)

    client = TestClient(app_module.app)
    r = client.post("/api/readiness/apply-tier-down", json={})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is False
    assert body.get("action") == "already_easy"


def test_apply_tier_down_404_when_no_plan(tmp_path, monkeypatch):
    _isolate_plan_dir(tmp_path, monkeypatch)
    client = TestClient(app_module.app)
    r = client.post("/api/readiness/apply-tier-down", json={})
    assert r.status_code == 404


def test_apply_tier_down_400_for_bad_date(tmp_path, monkeypatch):
    _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan(tmp_path)
    client = TestClient(app_module.app)
    r = client.post("/api/readiness/apply-tier-down",
                    json={"date": "not-a-date"})
    assert r.status_code == 400


def test_apply_tier_down_triggers_reforecast(tmp_path, monkeypatch):
    """Downstream propagation: ``tp.reforecast_dict`` must be invoked so
    subsequent sessions reflow against the new actual load."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan(plan_dir, session_type="vo2max")

    called: list = []
    real_reforecast = tp.reforecast_dict

    def _spy(plan_dict, *args, **kwargs):
        called.append(kwargs)
        return real_reforecast(plan_dict, *args, **kwargs)

    monkeypatch.setattr(tp, "reforecast_dict", _spy)

    client = TestClient(app_module.app)
    r = client.post("/api/readiness/apply-tier-down", json={})
    assert r.status_code == 200
    assert called, "reforecast_dict was not invoked by apply-tier-down"
    # The reforecast must receive availability_overrides — same contract
    # as save-availability / accept-redraw.
    assert "availability_overrides" in called[0]


def test_dashboard_has_apply_tier_down_handler():
    """Pin the frontend wiring."""
    dash = (Path(app_module.__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert "function applyReadinessTierDown" in dash or "async function applyReadinessTierDown" in dash
    assert "id=\"apply-tier-down-btn\"" in dash
    assert "/api/readiness/apply-tier-down" in dash
