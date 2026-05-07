"""v1.4.2 — `_enrich_plan_for_response` is mtime+size+today_iso-keyed cache.

Behaviour asserted:
1. Hit: two consecutive calls with the same plan file (no mutation) and
   same `today_iso` reuse the cache — uncached body runs only once.
2. Bust on mtime change: rewriting the plan file → next call recomputes.
3. Bust on `today_iso` change: same file, different iso → recomputes.
4. No persisted plan file → falls through to the uncached body (no
   crash, no cache entry).
5. The cached path still mutates the live `plan_dict` in place (same
   contract as v1.4.0).

Cache implementation is a process-local dict gated by mtime + st_size +
today_iso. Single-process FastAPI worker; plan writes are serialised
via `tp.plan_write_lock()`.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with a clean cache."""
    app_module._ENRICH_CACHE.clear()
    yield
    app_module._ENRICH_CACHE.clear()


def _mk_plan_dict(monday: date) -> dict:
    """Minimal 1-week plan dict with one session per day."""
    sessions = []
    for off in range(7):
        d = monday + timedelta(days=off)
        sessions.append({
            "day": d.isoformat(),
            "day_name": d.strftime("%a"),
            "session_type": "z2" if off in (1, 3) else "rest",
            "duration_min": 60 if off in (1, 3) else 0,
            "tss_estimate": 45 if off in (1, 3) else 0,
            "description": "" ,
            "zwo_file": "" ,
            "zwo_name": "",
            "status": "pending",
        })
    return {
        "goal": {"type": "weeks"},
        "weeks": [{
            "week_num": 1, "phase": "base",
            "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "tss_target": 200, "is_stepback": False,
            "sessions": sessions, "hit_per_week": 0,
        }],
        "availability": {},
    }


@pytest.fixture
def plan_on_disk(tmp_path, monkeypatch):
    """Write a minimal plan to a temp dir and patch _plan_dir to return it.
    Yields (plan_path, plan_dict, today_iso, monday).
    """
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monday = date.today() - timedelta(days=date.today().weekday())
    plan = _mk_plan_dict(monday)
    plan_path = plan_dir / "current_plan.json"
    plan_path.write_text(json.dumps(plan, default=str), encoding="utf-8")
    monkeypatch.setattr(app_module, "_plan_dir", lambda: plan_dir)
    return plan_path, plan, date.today().isoformat(), monday


def test_cache_hit_skips_uncached_body(plan_on_disk):
    """Two calls with no mutation between them → uncached body runs once."""
    _path, plan, today, _monday = plan_on_disk
    with patch.object(
        app_module, "_enrich_plan_for_response_uncached",
        wraps=app_module._enrich_plan_for_response_uncached,
    ) as spy:
        app_module._enrich_plan_for_response(plan, today)
        app_module._enrich_plan_for_response(plan, today)
        assert spy.call_count == 1, (
            f"second call must hit cache (uncached fired {spy.call_count}x)"
        )


def test_cache_busts_on_plan_mtime_change(plan_on_disk):
    """Mutating the plan file (mtime/size change) busts the cache."""
    path, plan, today, _monday = plan_on_disk
    with patch.object(
        app_module, "_enrich_plan_for_response_uncached",
        wraps=app_module._enrich_plan_for_response_uncached,
    ) as spy:
        app_module._enrich_plan_for_response(plan, today)
        # Bump mtime AND size by appending a comment-shaped key (any change
        # works — endpoint mutations rewrite the file via tmp+rename).
        plan["foo"] = "bar"
        path.write_text(json.dumps(plan, default=str), encoding="utf-8")
        # Force mtime to advance even on systems with low-resolution stat.
        future = time.time() + 1
        os.utime(path, (future, future))
        app_module._enrich_plan_for_response(plan, today)
        assert spy.call_count == 2, (
            "plan mtime change must bust cache; uncached called "
            f"{spy.call_count}x (expected 2)"
        )


def test_cache_busts_on_today_iso_rollover(plan_on_disk):
    """Same plan, different today_iso → recompute."""
    _path, plan, today, _monday = plan_on_disk
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    with patch.object(
        app_module, "_enrich_plan_for_response_uncached",
        wraps=app_module._enrich_plan_for_response_uncached,
    ) as spy:
        app_module._enrich_plan_for_response(plan, today)
        app_module._enrich_plan_for_response(plan, yesterday)
        assert spy.call_count == 2, (
            f"today_iso change must bust cache; got {spy.call_count}"
        )


def test_no_persisted_plan_falls_through(monkeypatch, tmp_path):
    """If `_plan_dir()/current_plan.json` doesn't exist, skip the cache."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(app_module, "_plan_dir", lambda: plan_dir)
    monday = date.today() - timedelta(days=date.today().weekday())
    plan = _mk_plan_dict(monday)
    today = date.today().isoformat()
    with patch.object(
        app_module, "_enrich_plan_for_response_uncached",
        wraps=app_module._enrich_plan_for_response_uncached,
    ) as spy:
        app_module._enrich_plan_for_response(plan, today)
        app_module._enrich_plan_for_response(plan, today)
        assert spy.call_count == 2, (
            "no persisted plan → cache must be skipped on every call"
        )
    # And no cache entries should be present.
    assert app_module._ENRICH_CACHE == {}, "cache must stay empty when no plan path"


def test_cached_path_still_mutates_plan_in_place(plan_on_disk):
    """v1.4.0 contract: caller's `plan_dict` is mutated in place. The cache
    layer must preserve that — even on a hit, the live dict gets the
    enrichment fields applied.
    """
    _path, plan, today, _monday = plan_on_disk
    # First call — uncached, writes fields onto plan.
    app_module._enrich_plan_for_response(plan, today)
    # Build a fresh dict (same shape, same days) and call again — should
    # be a cache hit AND should populate the new dict.
    fresh = _mk_plan_dict(date.fromisoformat(plan["weeks"][0]["start"]))
    fresh_session = fresh["weeks"][0]["sessions"][1]  # z2 day
    assert "card_state_v2" not in fresh_session, "fresh dict starts un-enriched"
    app_module._enrich_plan_for_response(fresh, today)
    assert "card_state_v2" in fresh_session, (
        "cache hit must still apply enrichment fields onto the caller's dict"
    )
