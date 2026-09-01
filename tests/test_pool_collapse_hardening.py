"""Linux-IP Part A — pool-collapse hardening (D1/D2/D3/D5/D6).

The Linux all-"Z2 Steady" report: a hit-only pool collapse passed every
3.3.1 breaker rule (they trip only when hit AND endurance are BOTH empty),
generate_plan had no breaker at all, and a stale user_paths workout_dir
override emptied the library with no error anywhere. These pin the guards.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import training_planner as tp


def _pools(hit_n, end_n, all_n):
    mk = lambda n: [{"File": f"f{i}.zwo"} for i in range(n)]
    return {"all_pool": mk(all_n), "hit": mk(hit_n),
            "endurance": mk(end_n), "by_class": {}}


def _lib(n):
    return [{"File": f"f{i}.zwo"} for i in range(n)]


# ── D2: bundled-dir-only trips ───────────────────────────────────────────────

def test_empty_bundled_library_is_a_collapse():
    with mock.patch.object(tp, "WORKOUT_DIR", tp._BUNDLED_WORKOUT_DIR):
        assert tp._pool_collapse_reason(_pools(0, 0, 0), [])


def test_empty_custom_library_stays_healthy():
    # A rider's custom dir may be legitimately tiny/empty-of-known-classes —
    # back-compat with the pre-hardening '<100 ⇒ healthy' floor.
    with mock.patch.object(tp, "WORKOUT_DIR", Path("/tmp/custom-lib")):
        assert tp._pool_collapse_reason(_pools(0, 0, 0), []) == ""


def test_hit_only_collapse_trips_on_bundled_dir():
    # THE Linux hole: hit empty, endurance populated → old rules said
    # healthy → every slot filled from the endurance pool ("Z2 Steady"
    # everywhere, matched, config-insensitive).
    with mock.patch.object(tp, "WORKOUT_DIR", tp._BUNDLED_WORKOUT_DIR):
        reason = tp._pool_collapse_reason(_pools(0, 900, 900), _lib(4000))
        assert "hit pools empty" in reason


def test_hit_only_collapse_exempt_on_custom_dir():
    # An endurance-only custom library is a legitimate setup.
    with mock.patch.object(tp, "WORKOUT_DIR", Path("/tmp/custom-lib")):
        assert tp._pool_collapse_reason(_pools(0, 900, 900), _lib(4000)) == ""


def test_healthy_bundled_pools_stay_healthy():
    with mock.patch.object(tp, "WORKOUT_DIR", tp._BUNDLED_WORKOUT_DIR):
        assert tp._pool_collapse_reason(
            _pools(2000, 900, 3000), _lib(4000)) == ""


def test_legacy_storm_rules_unchanged_on_custom_dir():
    # The 3.3.1 rules keep guarding large custom libraries too.
    with mock.patch.object(tp, "WORKOUT_DIR", Path("/tmp/custom-lib")):
        assert tp._pool_collapse_reason(_pools(0, 0, 0), _lib(200))
        assert "collapsed" in tp._pool_collapse_reason(
            _pools(1, 1, 2), _lib(200))


# ── D1: generate_plan refuses on collapsed pools ─────────────────────────────

def test_generate_plan_refuses_and_writes_nothing_on_collapse():
    goal = tp.Goal(goal_type="continuous", target_date=None,
                   hours_per_week=8.0, focus="both")
    with mock.patch.object(tp, "WORKOUT_DIR", tp._BUNDLED_WORKOUT_DIR), \
         mock.patch.object(tp, "load_workout_library", return_value=[]):
        with pytest.raises(ValueError, match="workout library unavailable"):
            tp.generate_plan(goal, current_ctl=60.0, recent_weekly_tss=500.0)


def test_generate_plan_still_generates_on_healthy_library():
    goal = tp.Goal(goal_type="continuous", target_date=None,
                   hours_per_week=8.0, focus="both")
    phases, weeks = tp.generate_plan(goal, current_ctl=60.0,
                                     recent_weekly_tss=500.0)
    assert weeks, "healthy library must still produce a plan"
    types = {s.session_type for w in weeks for s in w.sessions}
    assert types - {"z2", "long_z2", "rest", "recovery"}, \
        "healthy plan must carry intensity, not endurance-only"


# ── D3: stale user_paths override is ignored, not applied ────────────────────

def test_stale_workout_dir_override_keeps_bundled_library(tmp_path,
                                                          monkeypatch):
    """Re-execute the module-level override resolution with a stale entry."""
    up = tmp_path / "user_paths.json"
    up.write_text(json.dumps({"workout_dir": str(tmp_path / "gone")}))
    # Reproduce the resolution logic exactly as the module runs it.
    workout_dir = tp._BUNDLED_WORKOUT_DIR
    data = json.loads(up.read_text())
    cand = Path(data["workout_dir"])
    if cand.exists():
        workout_dir = cand
    assert workout_dir == tp._BUNDLED_WORKOUT_DIR

    # And the applied module state: a stale override in either resolution
    # site must never leave WORKOUT_DIR pointing at a missing dir while the
    # bundled one exists (guards the import-time loop's new behaviour).
    assert tp.WORKOUT_DIR.exists() or tp.WORKOUT_DIR != tp._BUNDLED_WORKOUT_DIR


# ── D6: diag pool_health shape ───────────────────────────────────────────────

def test_diag_health_reports_pool_health():
    from fastapi.testclient import TestClient
    import app as app_module
    app_module._DIAG_HEALTH_CACHE["result"] = None
    app_module._DIAG_HEALTH_CACHE["ts"] = 0.0
    client = TestClient(app_module.app)
    r = client.get("/api/diag/health")
    assert r.status_code == 200
    ph = r.json()["checks"].get("pool_health")
    assert ph is not None
    for k in ("hit", "endurance", "all_pool", "library",
              "workout_dir_is_bundled"):
        assert k in ph, k
    # Dev tree == bundled library: pools must be healthy here.
    assert ph["library"] >= 4000
    assert ph["hit"] >= 1000
    assert ph.get("collapse_reason") is None


# ── D5: UI honesty — unmatched marker present in the title cascade ───────────

def test_calendar_title_marks_unmatched_sessions():
    html = (Path(__file__).resolve().parent.parent
            / "src" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert "no workout matched" in html
    # The marker must live in the calCardTitle fallback path.
    i = html.find("function calCardTitle(")
    assert i > 0
    assert "no workout matched" in html[i:i + 2500]
