"""Regression tests for training.py ICU client reliability (fix26).

Covers:
- 3-attempt retry with exponential backoff on 5xx (§5.2)
- Retry-After honoured on 429 (§5.2)
- 401 raised as typed ICUAuthError (§5.1)
- 5 consecutive 401s flip auth_disabled in db._sync_loop (§5.1 + callers)
- manual-source W' is NOT overwritten by ICU sync (§5.4)
"""
from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch, MagicMock

import pytest

import training
from training import (
    ICUAuthError,
    ICURateLimitError,
    ICUServerError,
    ICUNetworkError,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _stub_credentials(monkeypatch):
    """Always supply dummy creds so _require_credentials passes."""
    monkeypatch.setattr(training.config, "ICU_ATHLETE_ID", "i1", raising=False)
    monkeypatch.setattr(training.config, "ICU_API_KEY", "k1", raising=False)
    monkeypatch.setattr(training.config, "ICU_BASE", "https://example.test", raising=False)
    yield


def _ok_response(payload):
    """Mimic urllib.request.urlopen context-manager return."""
    mock = MagicMock()
    mock.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return mock


def _http_error(code: int, headers: dict | None = None):
    return urllib.error.HTTPError(
        url="u", code=code, msg="x",
        hdrs=headers or {}, fp=io.BytesIO(b""),
    )


# ─── §5.2 Retry on 5xx ───────────────────────────────────────────────────────

def test_get_retries_on_5xx():
    """503, 503, 200 → success after 2 retries (3 attempts total)."""
    responses = [_http_error(503), _http_error(503), _ok_response({"ok": True})]

    def _side_effect(*_a, **_kw):
        r = responses.pop(0)
        if isinstance(r, urllib.error.HTTPError):
            raise r
        return r

    with patch("urllib.request.urlopen", side_effect=_side_effect):
        # Patch sleep where training resolves it (training.time is a
        # no-op-sleep proxy under conftest's hermetic gate, the real module
        # standalone — patch.object works for both).
        with patch.object(training.time, "sleep") as slp:
            result = training._get("athlete/i1/wellness")
    assert result == {"ok": True}
    # Two backoff sleeps (between the three attempts). Durations are
    # 2**attempt + random jitter → bounded within [1, 3).
    assert slp.call_count == 2
    for call in slp.call_args_list:
        delay = call.args[0]
        assert 1.0 <= delay < 3.0


# ─── §5.2 429 + Retry-After ──────────────────────────────────────────────────

def test_get_honors_retry_after_on_429():
    """429 with Retry-After: 5 → sleep(5) then retry and succeed."""
    err = _http_error(429, headers={"Retry-After": "5"})
    responses = [err, _ok_response({"ok": 1})]

    def _side_effect(*_a, **_kw):
        r = responses.pop(0)
        if isinstance(r, urllib.error.HTTPError):
            raise r
        return r

    with patch("urllib.request.urlopen", side_effect=_side_effect):
        with patch.object(training.time, "sleep") as slp:
            result = training._get("path")
    assert result == {"ok": 1}
    # Exactly one sleep, exactly 5 seconds (Retry-After value).
    assert slp.call_count == 1
    assert slp.call_args.args[0] == 5


def test_get_429_without_retry_after_raises_ratelimit():
    """429 with no Retry-After (and no budget) → ICURateLimitError."""
    err = _http_error(429, headers={})
    with patch("urllib.request.urlopen", side_effect=err):
        with patch("time.sleep"):
            with pytest.raises(ICURateLimitError):
                training._get("path")


# ─── §5.1 401 → typed ICUAuthError ───────────────────────────────────────────

def test_401_raises_ICUAuthError():
    """401 from ICU → ICUAuthError, propagates to callers."""
    err = _http_error(401)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(ICUAuthError):
            training._get("path")


def test_fetch_wellness_bubbles_ICUAuthError():
    """fetch_wellness must let ICUAuthError through to the sync loop."""
    err = _http_error(401)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(ICUAuthError):
            training.fetch_wellness(days=7)


# ─── Callers + db._sync_loop integration ─────────────────────────────────────

def test_401_trips_auth_disable_after_5_strikes(monkeypatch, tmp_path):
    """db._sync_loop flips _auth_disabled=True after 5 consecutive 401s."""
    import db
    dbfile = tmp_path / "t.db"
    db.set_db_path(dbfile)
    db.close_all_connections()
    db.init_db()
    # Reset health state.
    db._auth_disabled = False
    db._consecutive_failures = 0
    db._last_sync_error = None

    # Make run_sync raise ICUAuthError each call.
    def _raise(*_a, **_kw):
        raise ICUAuthError("HTTP 401")
    monkeypatch.setattr(db, "run_sync", _raise)
    # Break out of the sleep wait immediately so the loop spins.
    monkeypatch.setattr(db._sync_stop, "wait", lambda *_a, **_kw: None)
    # Stop the loop after 6 iterations (5 to flip + 1 to exit).
    calls = {"n": 0}
    real_is_set = db._sync_stop.is_set
    def _is_set():
        calls["n"] += 1
        # Exit after 10 iterations so the test can't hang.
        return calls["n"] > 10 or db._auth_disabled
    monkeypatch.setattr(db._sync_stop, "is_set", _is_set)

    db._sync_loop(interval_sec=1)
    assert db._auth_disabled is True
    assert db._consecutive_failures >= 5


# ─── §5.4 W' manual-source guard ─────────────────────────────────────────────

def test_w_prime_manual_source_not_overwritten_by_icu(tmp_path, monkeypatch):
    """User-logged W' (source=manual) must survive an ICU sync_wellness run."""
    import db
    dbfile = tmp_path / "t.db"
    db.set_db_path(dbfile)
    db.close_all_connections()
    db.init_db()

    # User manually logged W' for today.
    today = "2026-04-18"
    db.log_metric(today, "w_prime", 22000, source="manual")

    # ICU payload says W' is 30000 — should NOT overwrite the manual value.
    icu_payload = [{
        "id": today,
        "sportInfo": [{"wPrime": 30000, "eftp": 250}],
        "ctl": 50, "atl": 40,
    }]
    monkeypatch.setattr(db, "fetch_wellness", lambda days=90: icu_payload)

    db.sync_wellness(days=7)

    conn = db.get_db()
    row = conn.execute(
        "SELECT value, source FROM athlete_metrics WHERE date = ? AND metric = 'w_prime'",
        (today,),
    ).fetchone()
    assert row is not None
    assert row[0] == 22000, "manual W' must not be overwritten by ICU"
    assert row[1] == "manual", "source must remain 'manual'"


def test_w_prime_icu_source_is_overwritten_by_icu(tmp_path, monkeypatch):
    """If existing source is 'intervals.icu' (not manual), ICU sync may update it."""
    import db
    dbfile = tmp_path / "t.db"
    db.set_db_path(dbfile)
    db.close_all_connections()
    db.init_db()

    today = "2026-04-18"
    db.log_metric(today, "w_prime", 18000, source="intervals.icu")

    icu_payload = [{
        "id": today,
        "sportInfo": [{"wPrime": 25000}],
        "ctl": 50, "atl": 40,
    }]
    monkeypatch.setattr(db, "fetch_wellness", lambda days=90: icu_payload)

    db.sync_wellness(days=7)

    conn = db.get_db()
    row = conn.execute(
        "SELECT value, source FROM athlete_metrics WHERE date = ? AND metric = 'w_prime'",
        (today,),
    ).fetchone()
    assert row is not None
    assert row[0] == 25000
    assert row[1] == "intervals.icu"
