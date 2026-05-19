"""v1.7.4 — visible Sync button on the Plan tab.

User: "i reopen the app and have done some workouts this weekend. but
in current training plan theyre not there!"

Diagnosis: the auto-sync at boot fires once via the lazy ICU hook, but
its 1h throttle then blocks any subsequent sync within the same
session. The home-page header has a "Sync now ⟳" button that POSTs
``/api/rides/sync?force=1`` (bypasses the throttle) — but the user
spends most of their time on the Plan tab where the button was
invisible.

v1.7.4 wires the same handler to a Plan-tab button (``syncNowBtnPlan``)
and extends ``syncNowAction(btnId)`` to accept a button id so the
caller can choose which button to drive. Post-sync the handler now
also refreshes ``loadPlan()`` + ``loadWeeklyCalendar()`` (was only
``loadCalendar()``) so the Plan grid reflects newly-pulled actuals.
"""
from __future__ import annotations

from pathlib import Path

import app as app_module


def _dash() -> str:
    return (Path(app_module.__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")


def test_plan_tab_sync_button_present():
    dash = _dash()
    assert 'id="syncNowBtnPlan"' in dash, \
        "Plan-tab Sync button (syncNowBtnPlan) missing"
    assert "syncNowAction('syncNowBtnPlan')" in dash, \
        "Plan-tab Sync button must invoke syncNowAction with its own id"


def test_sync_action_accepts_button_id_parameter():
    """syncNowAction(btnId) must accept an optional id so non-home callers
    can drive the same flow without DOM duplication."""
    dash = _dash()
    assert "async function syncNowAction(btnId)" in dash, \
        "syncNowAction must accept a btnId parameter"
    assert "getElementById(btnId || 'syncNowBtn')" in dash, \
        "syncNowAction must default to 'syncNowBtn' when btnId omitted"


def test_sync_handler_refreshes_plan_and_weekly_views():
    """Post-sync the handler must refresh the Plan grid + weekly
    calendar in addition to /api/calendar. Without this the Plan tab's
    session cards stay stale until the user manually navigates away
    and back."""
    dash = _dash()
    # Locate the syncNowAction function body.
    start = dash.index("async function syncNowAction(btnId)")
    end = dash.index("function renderCalendarEmpty", start)
    body = dash[start:end]
    assert "await loadCalendar()" in body
    assert "await loadPlan()" in body
    assert "loadWeeklyCalendar()" in body


def test_rides_sync_force_endpoint_registered():
    """Smoke-pin: the underlying force-sync endpoint must exist."""
    routes = [getattr(r, "path", "") for r in app_module.app.routes]
    assert "/api/rides/sync" in routes
