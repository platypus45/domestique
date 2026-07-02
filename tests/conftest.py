"""Shared pytest fixtures for the Domestique test suite.

Order-independence guard for the lazy ICU sync singleton.

``app._kick_lazy_icu_sync`` fires its work on a daemon thread and uses the
module-level ``threading.Event`` ``app._icu_sync_in_progress`` as an
in-process "a sync is already running" guard:

    if _icu_sync_in_progress.is_set():
        return False          # <- next caller silently skips the sync
    _icu_sync_in_progress.set()
    # ... daemon thread clears it in a finally ...

Tests that assert the lazy sync *fired* (e.g. test_calendar_icu_sync.py,
test_lazy_icu_sync_endpoints.py) drive an endpoint and then check that
``training.fetch_recent_activities`` was called. If a *previous* test's
daemon sync thread is still in flight (or died before its ``finally``
cleared the Event) when the next test starts, the guard is still set, the
endpoint's ``_kick_lazy_icu_sync`` returns ``False`` immediately, the sync
never fires, and the assertion fails — but only depending on collection
order/timing, so the same test passes in isolation.

This autouse fixture drains any in-flight sync thread and clears the Event
after every test so the singleton can never leak across test boundaries.
"""
from __future__ import annotations

import threading
from datetime import date

import pytest

import app as app_module
import training_planner as _tp

# ── W8 (v2.5.0): shared planner-environment pinning ─────────────────────────
# generate_plan is DETERMINISTIC under a fixed seed_salt; the historic planner
# suite flakiness was ENVIRONMENT COUPLING — it self-fetches live CTL
# (get_today_metrics → ICU wellness), recent_mean_weekly_tss() from the live
# ~/.domestique archive, and anchors phase layout on date.today(). The planner
# suites (test_planner_diversification / _variety_bonus /
# _full_library_utilization / _interval_variety) pin all three via this block
# so the same plan is produced on any machine, any day.
#
# PLANNER_PIN_WEEKLY_TSS = 650 is the fixture-implied value (~10h/week × 65
# TSS/h, the legacy availability cap): the E1 ACWR ceiling becomes 650×1.3=845,
# above every phase tss_per_week target, so the full 24-week plan geometry the
# suites' acceptance thresholds were calibrated for is preserved. (A live
# archive in a low-volume period caps weeks at ~460 TSS and shrinks the plan
# to ~96 sessions — the flake vector.)
PLANNER_PIN_ANCHOR = date(2026, 1, 5)      # fixed Monday — frozen "today"
PLANNER_PIN_CTL = 50.0
PLANNER_PIN_WEEKLY_TSS = 650.0
# Splat into generate_plan calls: tp.generate_plan(goal, **PLANNER_PIN_ARGS)
PLANNER_PIN_ARGS = {
    "current_ctl": PLANNER_PIN_CTL,
    "recent_weekly_tss": PLANNER_PIN_WEEKLY_TSS,
}


class FrozenPlannerDate(date):
    @classmethod
    def today(cls):
        return cls(PLANNER_PIN_ANCHOR.year, PLANNER_PIN_ANCHOR.month,
                   PLANNER_PIN_ANCHOR.day)


@pytest.fixture(scope="module")
def planner_pinned_env():
    """Freeze date.today() inside training_planner and stub the live ICU
    metrics fetch (its result is unused once current_ctl is passed, but the
    call itself is a network round-trip per generate_plan). Request from a
    module-scoped autouse fixture in each planner suite."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_tp, "date", FrozenPlannerDate)
        mp.setattr(_tp, "get_today_metrics", lambda: {})
        yield


@pytest.fixture(autouse=True)
def _reset_icu_sync_singleton():
    yield
    # Drain a still-running lazy-sync daemon thread so it can't bleed into
    # the next test's patched paths/mocks, then clear the in-progress guard.
    for t in threading.enumerate():
        if t.name == "domestique.icu_sync" and t.is_alive():
            t.join(timeout=5.0)
    app_module._icu_sync_in_progress.clear()


@pytest.fixture(autouse=True)
def _no_live_icu_in_generate_plan(monkeypatch):
    """v3.0.0 hermetic gate: generate_plan's CTL self-fetch must never hit the
    live intervals.icu API from tests (a 429 retry-sleep hung the release gate;
    ~15 suites call generate_plan unpinned). Suites that pin current_ctl skip
    the fetch entirely (source fix); this stub covers the rest. Tests that
    exercise get_today_metrics itself target training.get_today_metrics and
    are unaffected; per-suite stubs simply override this one."""
    import training_planner as _tp
    monkeypatch.setattr(_tp, "get_today_metrics", lambda: {}, raising=False)
