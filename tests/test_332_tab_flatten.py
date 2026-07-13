"""3.3.2 (Lapo #2) — tab-visit auto-recalc flattened plans to Z2/taper-only.

Root cause chain (DIAG_TAB_FLATTEN): every goal persists event_date, the
readiness pct formula for non-event goals sits in the needs_adjustment band
at any CTL, and recalculate_plan taper-locked on taper_action alone — so
within 20 days of an FTP/general goal's target every auto-recalc rebuilt
the remaining plan as ONE taper phase. The gate re-armed every visit
because /api/plan/regenerate never stamped recalc_date, and a readiness
exception on the fresh branch silently converted "fresh" into a rebuild.
"""
from datetime import date, datetime, timedelta

import pytest

import training_planner as tp
import app as app_module


def _goal(gtype, days_out):
    return tp.Goal(
        goal_type=gtype,
        target_date=date.today() + timedelta(days=days_out),
        hours_per_week=8.0,
        plan_weeks=max(2, days_out // 7),
    )


# ── T1: non-event goals never taper-lock a recalc ──────────────────────────

@pytest.mark.parametrize("gtype", ["ftp", "general", "vo2max"])
def test_recalc_never_taper_locks_non_event_goal(gtype):
    """Inside 20 days of target, a non-event goal must keep its phase mix —
    the flatten was phases collapsing to [taper]."""
    goal = _goal(gtype, 14)
    phases, weeks = tp.generate_plan(
        goal, athlete={"ftp": 250, "weight_kg": 70}, recent_weekly_tss=350,
        seed_salt=3)
    # sanity: the generator itself never emits taper for these goals
    assert all(p.name != "taper" for p in phases)

    new_phases, all_weeks, info = tp.recalculate_plan(
        goal, list(weeks), current_ctl=45.0)
    if info.get("action") == "recalculated":
        names = [p.name for p in new_phases]
        assert names and names != ["taper"], (
            f"non-event {gtype} goal taper-locked on recalc: {names}")
        assert any(n != "taper" for n in names)


def test_recalc_can_still_taper_event_goal():
    """Event goals keep the taper-lock behavior (the fix must not remove
    legitimate event tapers)."""
    goal = tp.Goal(
        goal_type="event",
        target_date=date.today() + timedelta(days=12),
        event_km=160, event_climb_m=1500, event_type="gran_fondo",
        hours_per_week=8.0, plan_weeks=2,
    )
    phases, weeks = tp.generate_plan(
        goal, athlete={"ftp": 250, "weight_kg": 70}, recent_weekly_tss=350,
        seed_salt=3)
    new_phases, all_weeks, info = tp.recalculate_plan(
        goal, list(weeks), current_ctl=30.0)
    # No assertion on WHICH action fires (readiness-dependent) — only that
    # the code path is allowed to produce a taper for events.
    assert info.get("action") in ("recalculated", "no_change", "pool_collapse")


# ── T2: regenerate stamps recalc_date (gate closes) ────────────────────────

def _mk_week_plan(monday: date) -> dict:
    """Minimal current-week plan dict (borrowed shape from
    test_execution_score._mk_week_plan)."""
    sessions = []
    for d in range(7):
        day = monday + timedelta(days=d)
        stype = "rest" if d in (0, 4) else "z2"
        sessions.append({
            "day": day.isoformat(), "day_name": day.strftime("%a"),
            "session_type": stype,
            "duration_min": 0 if stype == "rest" else 60,
            "tss_estimate": 0 if stype == "rest" else 50,
            "description": "", "status": "pending",
            "zwo_file": "", "zwo_name": "",
        })
    return {"weeks": [{"start": monday.isoformat(),
                       "end": (monday + timedelta(days=6)).isoformat(),
                       "week_num": 1, "phase": "base", "tss_target": 300,
                       "sessions": sessions}]}


def test_regenerate_stamps_recalc_date():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    plan = _mk_week_plan(monday)
    plan["goal"] = {"type": "general", "hours_per_week": 6,
                    "event_date": (today + timedelta(weeks=6)).isoformat()}
    # deliberately stale stamps — the Lapo state
    plan["generated"] = "2026-01-01T00:00:00"
    plan["recalc_date"] = "2026-01-01T00:00:00"

    new_plan, _info = app_module._regenerate_plan_dict(
        plan, current_ctl=50.0, activities=[], seed_salt=1)

    assert new_plan.get("regenerated", "").startswith(str(today.year))
    stamped = new_plan.get("recalc_date", "")
    assert stamped and stamped != "2026-01-01T00:00:00", (
        "regen must refresh recalc_date or every Plan-tab visit re-runs "
        "auto-recalc forever")
    assert datetime.fromisoformat(stamped).date() == today


# ── T3: fresh-branch readiness failure stays fresh ─────────────────────────

def test_fresh_branch_readiness_exception_returns_fresh(monkeypatch, tmp_path):
    """A readiness/metrics exception on a FRESH plan must not trigger a
    rebuild — pre-fix, ValueError/TypeError fell through to the recalc arm."""
    pdir = tmp_path / "plans"
    pdir.mkdir()
    import json as _json
    plan = {
        "goal": {"type": "general", "hours_per_week": 8.0,
                 "event_date": "not-a-date"},   # forces ValueError in readiness
        "weeks": [], "phases": [],
        "recalc_date": datetime.now().isoformat(),  # FRESH
    }
    (pdir / "current_plan.json").write_text(_json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(tp, "PLAN_DIR", pdir)

    def _boom():
        raise ValueError("metrics down")
    monkeypatch.setattr(app_module, "get_today_metrics", _boom)

    from fastapi.testclient import TestClient
    client = TestClient(app_module.app, raise_server_exceptions=False)
    r = client.get("/api/plan/auto-recalc")
    assert r.status_code == 200
    body = r.json()
    assert body.get("action") == "fresh", (
        f"fresh plan must stay fresh on readiness failure, got {body.get('action')}")
    # and the stored plan was not rewritten
    stored = _json.loads((pdir / "current_plan.json").read_text(encoding="utf-8"))
    assert stored["weeks"] == [] and stored["phases"] == []
