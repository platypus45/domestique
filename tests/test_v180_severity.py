"""v1.8.0 §F1 — `compute_training_severity` Hooper-precedence helper.

Locked return shape (MASTER_DECISIONS_v180.md):
    {"score", "severity", "source", "reasons", "hooper_index", "tsb"}

Hooper precedence:
  Hooper >= 18 → severity=rest, source=hooper
  14 <= Hooper <= 17 → severity=tier_down, source=hooper
  Hooper < 14 → severity=normal, source=hooper

Otherwise (no Hooper row OR all four rating fields are 0):
  Readiness composite score < 3 → rest
  3 <= score < 5 → tier_down
  score >= 5 → normal
  score is None → severity=normal, source=insufficient
"""
from __future__ import annotations

from datetime import date as _date, timedelta as _td

import pytest

import db
import readiness_composite as rc


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture()
def fresh_db(tmp_path):
    """Disposable SQLite for severity tests."""
    dbfile = tmp_path / "severity_test.db"
    db.set_db_path(dbfile)
    db.close_all_connections()
    db.init_db()
    return dbfile


def _seed_hooper(day_iso: str, sleep_q: int, fatigue: int,
                 stress: int, soreness: int, mood: int = 4) -> None:
    db.upsert_daily_log(day_iso, sleep_q, fatigue, soreness, stress, mood)


def _seed_wellness(day_iso: str, ctl: float, atl: float) -> None:
    conn = db.get_db()
    conn.execute(
        "INSERT OR REPLACE INTO wellness "
        "(date, ctl, atl, hrv, rhr, sleep_secs, sleep_score, eftp, raw_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (day_iso, ctl, atl, 60.0, 50, 27000, 85, 250.0, "{}"),
    )
    conn.commit()


# ── Locked-shape sanity ─────────────────────────────────────────────────────

def test_return_shape_keys_locked(fresh_db):
    today = _date.today().isoformat()
    out = rc.compute_training_severity("rider", today)
    # Exactly these six keys, no extras.
    assert set(out.keys()) == {
        "score", "severity", "source", "reasons", "hooper_index", "tsb",
    }


# ── Hooper precedence ───────────────────────────────────────────────────────

def test_hooper_rest_band(fresh_db):
    today = _date.today().isoformat()
    # 5+5+5+5 = 20 ≥ 18
    _seed_hooper(today, sleep_q=5, fatigue=5, stress=5, soreness=5)
    out = rc.compute_training_severity("rider", today)
    assert out["source"] == "hooper"
    assert out["severity"] == "rest"
    assert out["hooper_index"] == 20
    assert out["score"] is None


def test_hooper_tier_down_band(fresh_db):
    today = _date.today().isoformat()
    # 4+4+4+4 = 16 → tier_down
    _seed_hooper(today, sleep_q=4, fatigue=4, stress=4, soreness=4)
    out = rc.compute_training_severity("rider", today)
    assert out["source"] == "hooper"
    assert out["severity"] == "tier_down"
    assert out["hooper_index"] == 16


def test_hooper_normal_band(fresh_db):
    today = _date.today().isoformat()
    # 3+3+3+3 = 12 → normal
    _seed_hooper(today, sleep_q=3, fatigue=3, stress=3, soreness=3)
    out = rc.compute_training_severity("rider", today)
    assert out["source"] == "hooper"
    assert out["severity"] == "normal"
    assert out["hooper_index"] == 12


def test_hooper_boundary_18_is_rest(fresh_db):
    # Exactly at the rest boundary.
    today = _date.today().isoformat()
    # 5+5+4+4 = 18
    _seed_hooper(today, sleep_q=5, fatigue=5, stress=4, soreness=4)
    out = rc.compute_training_severity("rider", today)
    assert out["severity"] == "rest"


def test_hooper_boundary_14_is_tier_down(fresh_db):
    # Exactly at the tier-down boundary.
    today = _date.today().isoformat()
    # 4+4+3+3 = 14
    _seed_hooper(today, sleep_q=4, fatigue=4, stress=3, soreness=3)
    out = rc.compute_training_severity("rider", today)
    assert out["severity"] == "tier_down"


# ── Fallback to readiness composite ─────────────────────────────────────────

def test_insufficient_history_when_no_hooper_and_no_wellness(fresh_db):
    today = _date.today().isoformat()
    out = rc.compute_training_severity("rider", today)
    # No Hooper, no wellness → composite score=None, severity=normal,
    # source=insufficient (per addendum §F1).
    assert out["source"] == "insufficient"
    assert out["severity"] == "normal"
    assert out["hooper_index"] is None
    assert out["score"] is None


def test_tsb_field_populated_when_wellness_present(fresh_db):
    today = _date.today().isoformat()
    _seed_wellness(today, ctl=50.0, atl=40.0)
    out = rc.compute_training_severity("rider", today)
    # TSB = ctl - atl = 10.0
    assert out["tsb"] == 10.0


# ── Don't reuse tp._hooper_index_today (addendum §F1 contract) ─────────────

def test_does_not_use_hooper_helper_zero_collapse(fresh_db):
    """`tp._hooper_index_today` returns 0 (not None) when row missing —
    severity helper queries daily_log directly and must NOT collapse a
    missing row into severity=normal-from-Hooper. Source should fall to
    composite/insufficient, NOT 'hooper'."""
    today = _date.today().isoformat()
    out = rc.compute_training_severity("rider", today)
    # No row → source is NOT 'hooper'.
    assert out["source"] != "hooper"
    assert out["hooper_index"] is None
