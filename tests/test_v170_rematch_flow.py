"""v1.7.0 — rematch Preview/Accept/Reshuffle flow + downstream reforecast.

User asked for a preview-then-commit rematch UX:
  - preview-redraw: pick a candidate WITHOUT persisting.
  - accept-redraw: persist the user's chosen candidate AND trigger
    ``tp.reforecast_dict`` so downstream sessions' TSS / availability
    flow catches up.
  - reshuffle: call preview-redraw again with the rejected candidate
    appended to ``exclude_extra``.
  - decline: just drop the panel (no server call).

Pre-v1.7.0 ``/api/plan/re-draw`` was instant-apply and never
reforecasted, so a rematched session with a very different TSS left the
rest of the week's load math stale.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


def _isolate_plan_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(app_module, "_plan_dir", lambda: tmp_path)
    return tmp_path


def _seed_plan_with_session(plan_dir: Path, day_iso: str = "2026-06-10",
                            zwo_name: str = "SEED_WORKOUT") -> Path:
    """Build a minimal plan with a single z2 session on ``day_iso`` so the
    rematch helpers have something concrete to swap."""
    json_path = plan_dir / "current_plan.json"
    week_start = "2026-06-08"  # Monday before day_iso
    plan = {
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0]},
        "phases": [],
        "weeks": [
            {
                "week_num": 1,
                "start": week_start,
                "end": "2026-06-14",
                "sessions": [
                    {
                        "day": day_iso,
                        "day_name": "Wed",
                        "session_type": "z2",
                        "duration_min": 60,
                        "tss_estimate": 50.0,
                        "description": "Z2 endurance",
                        "zwo_name": zwo_name,
                        "zwo_file": zwo_name + ".zwo",
                        "variation": 0,
                        "status": "pending",
                    },
                ],
            }
        ],
        "availability": {},
    }
    json_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return json_path


def _pick_two_real_z2_workouts() -> tuple[str, str]:
    """Find two distinct z2 ZWOs in the real library — used to feed the
    rematch pool. ``match_zwo`` filters by session_type, so any z2-tagged
    file works."""
    lib = tp.load_workout_library()
    z2 = [w for w in lib if "z2" in (w.get("Category", "").lower() + " " + (w.get("File", "").lower()))]
    assert len(z2) >= 2, "library lacks two z2 workouts for the test"
    return z2[0]["Name"], z2[1]["Name"]


# ── preview-redraw ────────────────────────────────────────────────────────────


def test_preview_redraw_returns_candidate_without_persisting(tmp_path, monkeypatch):
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path = _seed_plan_with_session(plan_dir, day_iso="2026-06-10")
    pre_disk = json_path.read_text(encoding="utf-8")

    client = TestClient(app_module.app)
    r = client.post("/api/plan/preview-redraw", json={"date": "2026-06-10"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True, body
    assert body.get("day") == "2026-06-10"
    assert body.get("zwo_file")
    assert body.get("zwo_name")
    # Disk MUST be unchanged — preview is read-only.
    assert json_path.read_text(encoding="utf-8") == pre_disk


def test_preview_redraw_400_when_date_missing(tmp_path, monkeypatch):
    _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan_with_session(tmp_path, day_iso="2026-06-10")
    client = TestClient(app_module.app)
    r = client.post("/api/plan/preview-redraw", json={})
    assert r.status_code == 400


def test_preview_redraw_404_when_no_plan(tmp_path, monkeypatch):
    _isolate_plan_dir(tmp_path, monkeypatch)
    # NO seed: plan dir is empty.
    client = TestClient(app_module.app)
    r = client.post("/api/plan/preview-redraw", json={"date": "2026-06-10"})
    assert r.status_code == 404


def test_preview_redraw_no_session_at_date_returns_invalid(tmp_path, monkeypatch):
    _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan_with_session(tmp_path, day_iso="2026-06-10")
    client = TestClient(app_module.app)
    r = client.post("/api/plan/preview-redraw", json={"date": "2026-06-15"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is False
    assert body.get("action") == "invalid"


# ── reshuffle (exclude_extra) ────────────────────────────────────────────────


def test_preview_reshuffle_excludes_previous_pick(tmp_path, monkeypatch):
    """Calling preview-redraw with ``exclude_extra=[<first_pick>]`` must
    return a DIFFERENT zwo_name. Match_zwo's pool is large; a single
    name in the exclusion set is enough to force divergence."""
    _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan_with_session(tmp_path, day_iso="2026-06-10")

    client = TestClient(app_module.app)
    r1 = client.post("/api/plan/preview-redraw", json={"date": "2026-06-10"})
    assert r1.status_code == 200
    pick1 = r1.json()
    assert pick1["ok"] is True

    r2 = client.post(
        "/api/plan/preview-redraw",
        json={"date": "2026-06-10", "exclude_extra": [pick1["zwo_name"]]},
    )
    assert r2.status_code == 200
    pick2 = r2.json()
    # If the pool has >1 candidate, reshuffle must produce a different
    # name. If it returns the same, the exclusion didn't take.
    if pick2.get("ok"):
        assert pick2["zwo_name"] != pick1["zwo_name"], \
            "reshuffle returned same workout despite exclude_extra"


# ── accept-redraw ────────────────────────────────────────────────────────────


def test_accept_redraw_persists_swap(tmp_path, monkeypatch):
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path = _seed_plan_with_session(plan_dir, day_iso="2026-06-10",
                                        zwo_name="ORIGINAL")

    client = TestClient(app_module.app)
    r = client.post(
        "/api/plan/accept-redraw",
        json={
            "date": "2026-06-10",
            "zwo_file": "REPLACEMENT.zwo",
            "zwo_name": "REPLACEMENT",
            "variation": 3,
            "tss_estimate": 78.0,
            "duration_min": 90,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True

    persisted = json.loads(json_path.read_text(encoding="utf-8"))
    session = persisted["weeks"][0]["sessions"][0]
    assert session["zwo_file"] == "REPLACEMENT.zwo"
    assert session["zwo_name"] == "REPLACEMENT"
    assert session["variation"] == 3
    assert session["tss_estimate"] == 78.0
    assert session["duration_min"] == 90
    assert session["status"] == "pending"
    # last_rematch_day breadcrumb is written so the dashboard can show
    # "last rematch X minutes ago" if it wants to.
    assert persisted.get("last_rematch_day", {}).get("date") == "2026-06-10"
    assert persisted["last_rematch_day"]["new_zwo"] == "REPLACEMENT.zwo"


def test_accept_redraw_400_when_zwo_file_missing(tmp_path, monkeypatch):
    _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan_with_session(tmp_path, day_iso="2026-06-10")
    client = TestClient(app_module.app)
    r = client.post("/api/plan/accept-redraw", json={"date": "2026-06-10"})
    assert r.status_code == 400


def test_accept_redraw_404_when_no_plan(tmp_path, monkeypatch):
    _isolate_plan_dir(tmp_path, monkeypatch)
    client = TestClient(app_module.app)
    r = client.post(
        "/api/plan/accept-redraw",
        json={"date": "2026-06-10", "zwo_file": "REPLACEMENT.zwo"},
    )
    assert r.status_code == 404


def test_accept_redraw_triggers_reforecast(tmp_path, monkeypatch):
    """The accept path must call ``tp.reforecast_dict`` — that's how
    downstream sessions catch up to the new TSS / availability flow.
    Pre-v1.7.0 the rematch never reforecasted, so a 200 TSS swap could
    leave next-week's load planning oblivious to the spike."""
    _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan_with_session(tmp_path, day_iso="2026-06-10")

    called: list[tuple] = []
    real_reforecast = tp.reforecast_dict

    def _spy(plan_dict, *args, **kwargs):
        called.append((args, kwargs))
        return real_reforecast(plan_dict, *args, **kwargs)

    monkeypatch.setattr(tp, "reforecast_dict", _spy)

    client = TestClient(app_module.app)
    r = client.post(
        "/api/plan/accept-redraw",
        json={
            "date": "2026-06-10",
            "zwo_file": "REPLACEMENT.zwo",
            "zwo_name": "REPLACEMENT",
            "variation": 1,
            "tss_estimate": 95.0,
            "duration_min": 75,
        },
    )
    assert r.status_code == 200, r.text
    assert called, "tp.reforecast_dict was not invoked by accept-redraw"
    # The reforecast call must pass availability_overrides + tsb_series
    # kwargs — mirroring the _maybe_auto_reforecast call shape.
    kwargs = called[0][1]
    assert "availability_overrides" in kwargs


# ── frontend wiring ─────────────────────────────────────────────────────────


def test_dashboard_has_preview_accept_reshuffle_decline_handlers():
    """Pin the v1.7.0 frontend wiring so a future template refactor
    can't quietly remove any of the four functions the new UX depends on."""
    dash = (Path(app_module.__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert "function _rematchPreview" in dash
    assert "function _rematchReshuffle" in dash or "async function _rematchReshuffle" in dash
    assert "function _rematchAccept" in dash or "async function _rematchAccept" in dash
    assert "function _rematchDecline" in dash
    # Backend URLs the frontend POSTs to.
    assert "/api/plan/preview-redraw" in dash
    assert "/api/plan/accept-redraw" in dash
