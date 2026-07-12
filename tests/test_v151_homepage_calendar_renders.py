"""v1.5.1 regression test — homepage + calendar render.

Locks the wire contract that the dashboard frontend depends on:

1. ``GET /api/plan`` returns the same JSON byte-for-byte across two
   consecutive calls. A drift between cache miss + cache hit (v1.4.2
   ``_enrich_plan_for_response`` cache, mtime-keyed) would manifest as
   a visual flicker → broken homepage on the second render.

2. Every plan session emits ``card_state_v2`` (10-state, v1.4.0 wire
   contract). The dashboard ``renderCalDay`` JS reads this field for
   per-cell tints. A renamed/dropped field would render every cell as
   the legacy 4-state fallback, losing the v1.4.1 finer tints.

3. Every plan session emits ``card_state`` (legacy 4-state). Frontend
   uses this for the rest/completed/missing/planned dispatch — drop it
   and every cell renders as REST.

4. ``GET /api/calendar`` returns a ``weeks`` list with at least one
   entry containing a ``days`` list of length 7. A schema break here
   collapses the bottom calendar to the empty-state.

These tests are a defensive boundary, not an investigation tool: they
catch v1.4.x/v1.5.x-style drifts before they ship.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import app
import training_planner as tp


@pytest.fixture(autouse=True)
def _sandbox_plan_dir(tmp_path, monkeypatch):
    """3.3.1 (gate-red postmortem): these tests read the LIVE profile plan
    via app._plan_dir() → tp.PLAN_DIR, with no sandbox. Under xdist, any
    plan-WRITING test in a sibling worker (save-availability, swap-type,
    reforecast persist) races the two /api/plan calls and the byte-identity
    assertion fails with a spurious availability/session diff — order-
    dependent, full-gate-only, solo-green. A synthetic two-week plan in a
    tmp dir makes the wire-contract checks hermetic; they never needed the
    real plan, only A plan."""
    monday = date.today() - timedelta(days=date.today().weekday())
    weeks = []
    for w in range(2):
        start = monday + timedelta(days=7 * w)
        sessions = []
        for d in range(7):
            day = start + timedelta(days=d)
            stype = "rest" if d in (0, 4) else ("threshold" if d == 2 else "z2")
            sessions.append({
                "day": day.isoformat(), "day_name": day.strftime("%a"),
                "session_type": stype,
                "duration_min": 0 if stype == "rest" else 60,
                "tss_estimate": 0 if stype == "rest" else 55,
                "description": "", "status": "pending",
                "zwo_file": "", "zwo_name": "",
            })
        weeks.append({"start": start.isoformat(),
                      "end": (start + timedelta(days=6)).isoformat(),
                      "week_num": w + 1, "phase": "base",
                      "tss_target": 300, "sessions": sessions})
    plan = {"goal": {"type": "general", "plan_weeks": 2},
            "availability": {}, "weeks": weeks,
            "generated": "2026-01-05T00:00:00"}
    pdir = tmp_path / "plans"
    pdir.mkdir()
    (pdir / "current_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(tp, "PLAN_DIR", pdir)
    app._ENRICH_CACHE.clear()
    yield
    app._ENRICH_CACHE.clear()


def test_api_plan_stable_across_two_calls() -> None:
    """Cache miss + cache hit must produce identical JSON.

    Reproduces the v1.5.1 suspect-(a)/(b) failure mode: if
    ``_apply_cached_enrichment`` strips fields not in
    ``_ENRICH_FIELDS``, the second response loses keys and the
    frontend renders empty cells.
    """
    # Clear cache to force first call through uncached path.
    app._ENRICH_CACHE.clear()
    c = TestClient(app.app)
    r1 = c.get("/api/plan")
    r2 = c.get("/api/plan")
    assert r1.status_code == 200
    assert r2.status_code == 200
    j1 = json.dumps(r1.json(), sort_keys=True, default=str)
    j2 = json.dumps(r2.json(), sort_keys=True, default=str)
    assert j1 == j2, "Cache hit response drifted from cache miss response"


def test_api_plan_card_state_v2_present_on_every_session() -> None:
    """Every plan session must carry ``card_state_v2``.

    Regression guard for v1.4.1 dashboard.html ``renderCalDay`` which
    reads ``d.card_state_v2`` for the per-cell tint mapping. Drops or
    renames here lose the v1.4.0 10-state UI tier silently.
    """
    c = TestClient(app.app)
    r = c.get("/api/plan")
    assert r.status_code == 200
    plan = (r.json() or {}).get("plan_json") or {}
    weeks = plan.get("weeks") or []
    if not weeks:
        # No plan persisted; nothing to validate.
        return
    saw_session = False
    for w in weeks:
        for s in (w.get("sessions") or []):
            saw_session = True
            assert "card_state_v2" in s, (
                f"session missing card_state_v2: day={s.get('day')}"
            )
            assert "card_state" in s, (
                f"session missing card_state: day={s.get('day')}"
            )
    assert saw_session, "no sessions present in plan; cannot validate fields"


def test_api_calendar_emits_weeks_with_seven_days() -> None:
    """``/api/calendar`` must yield weeks with 7 days each.

    Frontend ``renderCalendar`` iterates exactly 7 cells per row; a
    short week collapses the row to skeleton. ``card_state`` per day
    must also be present so the dispatch in ``renderCalDay`` works.
    """
    c = TestClient(app.app)
    r = c.get("/api/calendar")
    assert r.status_code == 200
    body = r.json()
    weeks = body.get("weeks") or []
    assert len(weeks) > 0, "calendar payload has no weeks"
    for w in weeks:
        days = w.get("days") or []
        assert len(days) == 7, (
            f"week starting {w.get('start_date')} has {len(days)} days, expected 7"
        )
        for d in days:
            assert "card_state" in d, (
                f"calendar day {d.get('date')} missing card_state"
            )


def test_api_plan_stable_after_cache_eviction() -> None:
    """Even when the cache is mid-eviction (>4 entries), responses
    stay shape-stable. Guards against the LRU pop dropping the live
    snapshot mid-request.
    """
    c = TestClient(app.app)
    # Pollute cache with bogus entries to force eviction on next miss.
    app._ENRICH_CACHE.clear()
    for i in range(6):
        app._ENRICH_CACHE[f"bogus-{i}"] = (float(i), {})
    r1 = c.get("/api/plan")
    r2 = c.get("/api/plan")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json(), (
        "Plan response drifted after cache eviction stress"
    )
