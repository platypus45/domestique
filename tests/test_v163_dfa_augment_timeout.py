"""v1.6.3 — _augment_icu_record_with_dfa has a hard 5 s timeout.

Without this cap a single slow ICU response or a >10k-record FIT parse
could hold the background sync thread for minutes; pre-v1.6.3 (when the
sync was synchronous) that hung the request thread and froze the
dashboard. The timeout boundary is enforced via
``concurrent.futures.ThreadPoolExecutor.submit(...).result(timeout=_DFA_AUGMENT_TIMEOUT_S)``.

Tests use a ``threading.Event`` instead of ``time.sleep`` so the slow
worker can be released cleanly after the assertion; otherwise the
non-daemon executor thread would block pytest's process exit.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import app


def _write_record(tmp_path: Path) -> Path:
    rec_path = tmp_path / "icu_42.json"
    rec_path.write_text(json.dumps({
        "id": "42",
        "started_at": "2026-05-13T08:00:00Z",
        "finished_at": "2026-05-13T09:00:00Z",
    }), encoding="utf-8")
    return rec_path


def test_augment_returns_within_timeout_on_slow_fetch(tmp_path, monkeypatch):
    rec_path = _write_record(tmp_path)
    release = threading.Event()

    def _hang_fetch(_external_id):
        # Block until released; the test releases after asserting timeout.
        release.wait(timeout=10)
        return b"never reached"

    monkeypatch.setattr("training.fetch_activity_fit_file", _hang_fetch)
    monkeypatch.setattr(app, "_DFA_AUGMENT_TIMEOUT_S", 0.5)

    try:
        t0 = time.time()
        app._augment_icu_record_with_dfa(rec_path, "42")
        elapsed = time.time() - t0

        # Hard cap is 0.5 s; tolerate executor teardown up to 2 s.
        assert elapsed < 2.0, f"timeout took {elapsed:.2f}s (>2.0s)"

        persisted = json.loads(rec_path.read_text(encoding="utf-8"))
        assert persisted["dfa_alpha1_status"] == "timeout"
        assert persisted["dfa_alpha1_avg"] is None
        assert persisted["dfa_alpha1_series"] == []
    finally:
        release.set()  # let the leaked worker thread exit cleanly


def test_augment_marks_timeout_status_on_slow_parse(tmp_path, monkeypatch):
    """Fetch succeeds quickly, but parse hangs -> timeout status."""
    rec_path = _write_record(tmp_path)
    release = threading.Event()

    monkeypatch.setattr("training.fetch_activity_fit_file", lambda _x: b"FITDATA")

    def _hang_parse(_p):
        release.wait(timeout=10)
        return {"dfa_alpha1_avg": 0.9}

    monkeypatch.setattr("analytics.compute_dfa_alpha1_for_fit", _hang_parse)
    monkeypatch.setattr(app, "_DFA_AUGMENT_TIMEOUT_S", 0.5)

    try:
        t0 = time.time()
        app._augment_icu_record_with_dfa(rec_path, "42")
        elapsed = time.time() - t0

        assert elapsed < 2.0
        persisted = json.loads(rec_path.read_text(encoding="utf-8"))
        assert persisted["dfa_alpha1_status"] == "timeout"
    finally:
        release.set()


def test_augment_normal_path_unaffected(tmp_path, monkeypatch):
    """Fast fetch + parse: result is persisted, status is propagated."""
    rec_path = _write_record(tmp_path)

    monkeypatch.setattr("training.fetch_activity_fit_file", lambda _x: b"FITDATA")
    monkeypatch.setattr(
        "analytics.compute_dfa_alpha1_for_fit",
        lambda _p: {
            "dfa_alpha1_avg": 0.85,
            "dfa_alpha1_series": [0.8, 0.9],
            "dfa_alpha1_lt1_minutes": 12,
            "rr_intervals_count": 100,
            "dfa_alpha1_status": "computed",
        },
    )

    app._augment_icu_record_with_dfa(rec_path, "42")

    persisted = json.loads(rec_path.read_text(encoding="utf-8"))
    assert persisted["dfa_alpha1_status"] == "computed"
    assert persisted["dfa_alpha1_avg"] == 0.85
    assert persisted["rr_intervals_count"] == 100
