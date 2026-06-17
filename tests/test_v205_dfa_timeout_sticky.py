"""v2.0.5 — a 'timeout' DFA record must not be re-hammered on every sync.

Rides with no recoverable RR data time out even at the full 45 s cap; the
pre-v2.0.5 code left status='timeout' permanently retry-eligible, so every
foreground sync re-attempted them (45 s each). With several such records the
loop ran for minutes — past the Plan-tab catch-up overlay's 40 s per-step
ceiling — which stranded the overlay and skipped the reconcile that adapts
the week to a missed session.

v2.0.5 fix:
  * retries of an existing 'timeout' record use a short cap
    (_DFA_AUGMENT_RETRY_TIMEOUT_S) instead of the full 45 s,
  * each timeout bumps ``dfa_timeout_count``,
  * after _DFA_MAX_TIMEOUT_RETRIES the status goes sticky (skipped, no
    network) — like ``icu_deleted`` — and auto-heals on a _DFA_ALGO_VERSION bump,
  * fresh records (and force=backfill) keep the full 45 s cap so legit
    heavy-FIT computes are not regressed.

Hangs use a ``threading.Event`` (not ``time.sleep``) so the leaked worker
thread can be released after the assertion, matching test_v163_dfa_augment_timeout.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import app


def _write_record(tmp_path: Path, **extra) -> Path:
    rec = {"id": "77", "started_at": "2026-05-13T08:00:00Z"}
    rec.update(extra)
    rec_path = tmp_path / "icu_77.json"
    rec_path.write_text(json.dumps(rec), encoding="utf-8")
    return rec_path


def test_timeout_record_is_sticky_after_max_retries(tmp_path, monkeypatch):
    """count >= cap -> skip ALL work (no network), status unchanged."""
    rec_path = _write_record(
        tmp_path,
        dfa_alpha1_status="timeout",
        dfa_timeout_count=app._DFA_MAX_TIMEOUT_RETRIES,
        dfa_algo_version=app._DFA_ALGO_VERSION,
    )
    called = {"fetch": False, "streams": False}

    def _should_not_fetch(_x):
        called["fetch"] = True
        return b"x"

    def _should_not_stream(_x):
        called["streams"] = True
        return {}

    monkeypatch.setattr("training.fetch_activity_fit_file", _should_not_fetch)
    monkeypatch.setattr("training.fetch_activity_streams", _should_not_stream)

    app._augment_icu_record_with_dfa(rec_path, "77")

    assert called["fetch"] is False, "sticky 'timeout' record should not fetch the FIT"
    assert called["streams"] is False, "sticky 'timeout' record should not hit ICU streams"
    persisted = json.loads(rec_path.read_text(encoding="utf-8"))
    assert persisted["dfa_alpha1_status"] == "timeout"
    assert persisted["dfa_timeout_count"] == app._DFA_MAX_TIMEOUT_RETRIES


def test_timeout_retry_uses_short_cap_and_increments(tmp_path, monkeypatch):
    """count < cap -> retried, but with the SHORT cap, and count bumps by 1."""
    rec_path = _write_record(
        tmp_path,
        dfa_alpha1_status="timeout",
        dfa_timeout_count=0,
        dfa_algo_version=app._DFA_ALGO_VERSION,
    )
    release = threading.Event()

    def _hang_fetch(_x):
        release.wait(timeout=10)
        return b"never reached"

    monkeypatch.setattr("training.fetch_activity_streams", lambda _x: {})
    monkeypatch.setattr("training.fetch_activity_fit_file", _hang_fetch)
    # Long base cap, short retry cap: a fast bail proves the retry cap was used.
    monkeypatch.setattr(app, "_DFA_AUGMENT_TIMEOUT_S", 10.0)
    monkeypatch.setattr(app, "_DFA_AUGMENT_RETRY_TIMEOUT_S", 0.5)

    try:
        t0 = time.time()
        app._augment_icu_record_with_dfa(rec_path, "77")
        elapsed = time.time() - t0
        assert elapsed < 2.0, f"retry used the long base cap ({elapsed:.2f}s) — short cap not honored"
        persisted = json.loads(rec_path.read_text(encoding="utf-8"))
        assert persisted["dfa_alpha1_status"] == "timeout"
        assert persisted["dfa_timeout_count"] == 1
    finally:
        release.set()


def test_fresh_record_keeps_full_cap(tmp_path, monkeypatch):
    """A fresh record must NOT be downgraded to the short retry cap — legit
    heavy-FIT computes still get the full base cap."""
    rec_path = _write_record(tmp_path)  # no prior dfa status
    release = threading.Event()

    def _hang_fetch(_x):
        release.wait(timeout=10)
        return b"never reached"

    monkeypatch.setattr("training.fetch_activity_streams", lambda _x: {})
    monkeypatch.setattr("training.fetch_activity_fit_file", _hang_fetch)
    monkeypatch.setattr(app, "_DFA_AUGMENT_TIMEOUT_S", 0.8)
    monkeypatch.setattr(app, "_DFA_AUGMENT_RETRY_TIMEOUT_S", 0.1)

    try:
        t0 = time.time()
        app._augment_icu_record_with_dfa(rec_path, "77")
        elapsed = time.time() - t0
        # ~0.8 s (full cap), not ~0.1 s (retry cap). Lower bound is the proof.
        assert elapsed >= 0.6, f"fresh record used the short retry cap ({elapsed:.2f}s)"
        assert elapsed < 2.5
        persisted = json.loads(rec_path.read_text(encoding="utf-8"))
        assert persisted["dfa_alpha1_status"] == "timeout"
        assert persisted["dfa_timeout_count"] == 1
    finally:
        release.set()


def test_force_backfill_ignores_sticky(tmp_path, monkeypatch):
    """force=True (the backfill path) must re-attempt even a sticky record and
    use the full cap — the user explicitly asked to recompute."""
    rec_path = _write_record(
        tmp_path,
        dfa_alpha1_status="timeout",
        dfa_timeout_count=app._DFA_MAX_TIMEOUT_RETRIES,
        dfa_algo_version=app._DFA_ALGO_VERSION,
    )
    called = {"streams": False}

    def _stream(_x):
        called["streams"] = True
        return {}

    monkeypatch.setattr("training.fetch_activity_streams", _stream)
    monkeypatch.setattr("training.fetch_activity_fit_file", lambda _x: None)

    app._augment_icu_record_with_dfa(rec_path, "77", force=True)

    assert called["streams"] is True, "force=backfill must re-attempt a sticky record"
