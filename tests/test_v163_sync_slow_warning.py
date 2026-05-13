"""v1.6.3 — slow background syncs surface E_SYNC_BLOCKING_SLOW.

Diagnostics modal previously rendered all-green even when a sync took
30+ s because no exception path fired -- the sync was just slow. The
new ``E_SYNC_BLOCKING_SLOW`` (WARN) is emitted by the background runner
when wall-clock elapsed exceeds ``_SYNC_SLOW_THRESHOLD_S``.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import app
import error_codes


def test_slow_sync_emits_warning_code(monkeypatch):
    app._icu_sync_in_progress.clear()

    # Tighten threshold to keep the test fast.
    monkeypatch.setattr(app, "_SYNC_SLOW_THRESHOLD_S", 0.2)

    def _slow_sync(force_if_today_missing: bool = False) -> None:
        time.sleep(0.4)

    captured: list[tuple] = []

    def _spy(code, **ctx):
        captured.append((code, ctx))

    with patch.object(app, "_maybe_lazy_icu_sync", side_effect=_slow_sync), \
         patch.object(app, "_log_error", side_effect=_spy):
        app._kick_lazy_icu_sync(force_if_today_missing=False)
        # Wait for the runner to finish
        for _ in range(50):
            if not app._icu_sync_in_progress.is_set():
                break
            time.sleep(0.05)

    codes = [c for c, _ in captured]
    assert error_codes.Codes.SYNC_BLOCKING_SLOW in codes, \
        f"expected E_SYNC_BLOCKING_SLOW in {codes}"

    ctx = next(ctx for c, ctx in captured if c == error_codes.Codes.SYNC_BLOCKING_SLOW)
    assert ctx.get("ms") is not None
    assert ctx["ms"] >= 200  # at least the threshold of 0.2 s in ms


def test_fast_sync_does_not_warn(monkeypatch):
    """A sync that finishes within the threshold must not emit the WARN."""
    app._icu_sync_in_progress.clear()
    monkeypatch.setattr(app, "_SYNC_SLOW_THRESHOLD_S", 1.0)

    def _fast_sync(force_if_today_missing: bool = False) -> None:
        time.sleep(0.01)

    captured: list = []

    def _spy(code, **ctx):
        captured.append(code)

    with patch.object(app, "_maybe_lazy_icu_sync", side_effect=_fast_sync), \
         patch.object(app, "_log_error", side_effect=_spy):
        app._kick_lazy_icu_sync(force_if_today_missing=False)
        for _ in range(50):
            if not app._icu_sync_in_progress.is_set():
                break
            time.sleep(0.05)

    assert error_codes.Codes.SYNC_BLOCKING_SLOW not in captured


def test_slow_warning_registered_in_registry():
    """Contract: every Codes constant has a REGISTRY row (v1.6.0 invariant)."""
    assert error_codes.Codes.SYNC_BLOCKING_SLOW in error_codes.REGISTRY
    meta = error_codes.REGISTRY[error_codes.Codes.SYNC_BLOCKING_SLOW]
    assert meta["severity"] == "WARN"
    assert meta["description"]
    assert meta["user_action"]
