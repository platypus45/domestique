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

import pytest

import app as app_module


@pytest.fixture(autouse=True)
def _reset_icu_sync_singleton():
    yield
    # Drain a still-running lazy-sync daemon thread so it can't bleed into
    # the next test's patched paths/mocks, then clear the in-progress guard.
    for t in threading.enumerate():
        if t.name == "domestique.icu_sync" and t.is_alive():
            t.join(timeout=5.0)
    app_module._icu_sync_in_progress.clear()
