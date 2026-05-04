"""Backend wellness-endpoint regression tests.

Separate from the broader readiness/training tests because the bugs covered
here are pinpoint fixes on `app.api_wellness` and `training.get_today_metrics`:
the previous truthy check treated `ctl=0` / `atl=0` (which training.py
explicitly special-cases for fresh-athlete data) as "missing" and emitted
`tsb=None` instead of the correct `0.0`.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as am
import db


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """FastAPI TestClient with a disposable SQLite and no live ICU pulls."""
    # Point db at a tmp file so we don't stomp the user's DB.
    dbfile = tmp_path / "health_tracker.db"
    db.set_db_path(dbfile)
    db.close_all_connections()
    db.init_db()
    # Stub the ICU fetch so /api/wellness uses the SQLite fallback branch.
    monkeypatch.setattr("app.fetch_wellness", lambda days: [])
    # Drop the in-process cache from any prior test.
    am.clear_cache()
    return TestClient(am.app)


def test_wellness_tsb_zero_when_ctl_and_atl_zero(client, monkeypatch):
    """ctl=0, atl=0 must yield tsb=0.0, not None.

    Training.py line 111 treats ctl=0 as sentinel "fresh athlete" data;
    the /api/wellness SQLite branch mirrored the intervals.icu branch but
    used a truthy check instead of `is not None`, which silently dropped
    these rows' TSB to None even though 0 − 0 = 0 is physiologically valid.
    """
    # Use a date inside the default 28-day lookup window.
    from datetime import date as _date
    target = _date.today().isoformat()

    import json as _json
    conn = db.get_db()
    conn.execute(
        "INSERT OR REPLACE INTO wellness (date, ctl, atl, hrv, rhr, sleep_secs, sleep_score, eftp, raw_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (target, 0, 0, None, None, None, None, None, _json.dumps({})),
    )
    conn.commit()

    resp = client.get("/api/wellness?days=28")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    row = next((r for r in rows if r.get("date") == target), None)
    assert row is not None, f"seeded row missing from response: {rows}"
    # The regression: legacy truthy check returned None here.
    assert row["tsb"] == 0.0, f"expected tsb=0.0 for ctl=atl=0, got {row['tsb']!r}"


def test_wellness_tsb_none_when_ctl_missing(client):
    """None inputs must still propagate to None — the fix must not break
    the existing "missing data → null TSB" path."""
    from datetime import date as _date, timedelta as _td
    target = (_date.today() - _td(days=1)).isoformat()

    conn = db.get_db()
    conn.execute(
        "INSERT OR REPLACE INTO wellness (date, ctl, atl, hrv, rhr, sleep_secs, sleep_score, eftp, raw_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (target, None, 5.0, None, None, None, None, None, "{}"),
    )
    conn.commit()

    resp = client.get("/api/wellness?days=28")
    assert resp.status_code == 200
    rows = resp.json()
    row = next((r for r in rows if r.get("date") == target), None)
    assert row is not None
    assert row["tsb"] is None
