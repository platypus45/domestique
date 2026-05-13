"""v1.6.3 — lazy ICU sync runs in a background thread.

Pre-v1.6.3 the three frontpage endpoints called ``_maybe_lazy_icu_sync``
synchronously on the request thread. When ICU had many new rides to
fetch + parse the request hung for >30 s and the dashboard rendered an
infinite loading spinner.

v1.6.3 replaces the direct call with ``_kick_lazy_icu_sync`` which
spawns a daemon thread, sets an ``Event`` to dedupe concurrent kicks,
and returns immediately.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import app


def test_kick_returns_immediately_with_slow_sync():
    """A 5 s sync must not block the caller for more than ~0.2 s."""
    app._icu_sync_in_progress.clear()

    def _slow_sync(force_if_today_missing: bool = False) -> None:
        time.sleep(5.0)

    with patch.object(app, "_maybe_lazy_icu_sync", side_effect=_slow_sync):
        t0 = time.time()
        started = app._kick_lazy_icu_sync(force_if_today_missing=True)
        elapsed = time.time() - t0

    assert started is True
    assert elapsed < 0.5, f"_kick_lazy_icu_sync blocked for {elapsed:.2f}s"

    # tidy: wait for the background thread to release the event
    for _ in range(60):
        if not app._icu_sync_in_progress.is_set():
            break
        time.sleep(0.1)
    assert not app._icu_sync_in_progress.is_set()


def test_kick_dedupes_concurrent_calls():
    """A second kick while one is in flight must return False (no thread storm)."""
    app._icu_sync_in_progress.clear()
    started_n = 0

    def _slow_sync(force_if_today_missing: bool = False) -> None:
        time.sleep(0.5)

    with patch.object(app, "_maybe_lazy_icu_sync", side_effect=_slow_sync):
        first = app._kick_lazy_icu_sync(force_if_today_missing=True)
        # Immediately try a second kick — the Event should be set.
        second = app._kick_lazy_icu_sync(force_if_today_missing=True)
        third = app._kick_lazy_icu_sync(force_if_today_missing=False)
        started_n = sum([first, second, third])

    assert started_n == 1, f"expected exactly one thread started, got {started_n}"

    # tidy
    for _ in range(60):
        if not app._icu_sync_in_progress.is_set():
            break
        time.sleep(0.1)


def test_kick_runs_sync_in_non_main_thread():
    """Verify the actual sync function executes off the calling thread."""
    app._icu_sync_in_progress.clear()
    captured: dict = {}

    def _capture(force_if_today_missing: bool = False) -> None:
        captured["thread_name"] = threading.current_thread().name
        captured["is_main"] = threading.current_thread() is threading.main_thread()

    with patch.object(app, "_maybe_lazy_icu_sync", side_effect=_capture):
        app._kick_lazy_icu_sync(force_if_today_missing=False)
        # Wait for completion
        for _ in range(50):
            if not app._icu_sync_in_progress.is_set():
                break
            time.sleep(0.1)

    assert captured.get("is_main") is False
    assert "icu_sync" in captured.get("thread_name", "")


def test_kick_clears_event_on_exception():
    """A raise inside the sync must still clear the Event so future kicks work."""
    app._icu_sync_in_progress.clear()

    def _raise(force_if_today_missing: bool = False) -> None:
        raise RuntimeError("boom")

    with patch.object(app, "_maybe_lazy_icu_sync", side_effect=_raise):
        app._kick_lazy_icu_sync(force_if_today_missing=False)
        # Wait for runner to handle exception and clear the Event
        for _ in range(50):
            if not app._icu_sync_in_progress.is_set():
                break
            time.sleep(0.1)

    assert not app._icu_sync_in_progress.is_set(), \
        "Event must be cleared even when sync raises"
