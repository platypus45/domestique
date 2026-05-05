"""v1.1.0 IMPL-HRV-RECOVERY — Bayesian HRV-readiness composite tests.

Locked contracts (PATCH G7, G13, G16) verified here:
  1. < 30 days wellness → score=None, status='insufficient_data'.
  2. 30-59 days → static weights, status='static_weights', no Bayesian update.
  3. ≥ 60 days → dynamic weights, weights diverge from initial after update.
  4. All-positive z-score inputs (≥60 d) → score in [7, 10].
  5. All-depressed inputs → score in [0, 3].
  6. Missing dfa_alpha1_y → re-normalisation works, confidence reduced.
  7. HRV4Training CSV upload: 7 days of valid rows → wellness.hrv populated;
     rows missing date or rmssd are skipped.
"""
from __future__ import annotations

import io
import json
import math
from datetime import date as _date, timedelta as _td
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import db
import readiness_composite as rc
import app as app_module


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Disposable SQLite + cleared in-process cache."""
    dbfile = tmp_path / "rc_test.db"
    db.set_db_path(dbfile)
    db.close_all_connections()
    db.init_db()
    monkeypatch.setattr("app.fetch_wellness", lambda days: [])
    app_module.clear_cache()
    return dbfile


@pytest.fixture()
def client(fresh_db):
    return TestClient(app_module.app)


def _seed_wellness(target: _date, days: int, hrv_base: float = 60.0,
                    hrv_jitter: float = 5.0, ctl: float = 50.0,
                    atl: float = 45.0, eftp: float | None = 250.0,
                    rng_seed: int = 42) -> None:
    """Seed `days` of wellness rows ending at `target` (inclusive).

    `hrv_base` is the baseline rMSSD; rows alternate ±jitter for a small SD.
    `eftp` is constant unless None.
    """
    import random
    rng = random.Random(rng_seed)
    conn = db.get_db()
    for i in range(days):
        d_iso = (target - _td(days=days - 1 - i)).isoformat()
        offset = rng.uniform(-hrv_jitter, hrv_jitter)
        hrv = max(10.0, hrv_base + offset)
        # ctl/atl mild walk, capped:
        ctl_v = ctl + rng.uniform(-1.0, 1.0)
        atl_v = atl + rng.uniform(-1.0, 1.0)
        conn.execute(
            "INSERT OR REPLACE INTO wellness "
            "(date, ctl, atl, hrv, rhr, sleep_secs, sleep_score, eftp, raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (d_iso, ctl_v, atl_v, hrv, 50, 27000, 85, eftp, "{}"),
        )
    conn.commit()


def _seed_daily_log(target: _date, days: int,
                    sleep_q: int = 4, fatigue: int = 4,
                    soreness: int = 4, stress: int = 4, mood: int = 4,
                    jitter: bool = True, rng_seed: int = 99) -> None:
    """Seed `days` of daily_log rows ending at `target` (inclusive).

    When `jitter=True`, introduces small ±1 perturbations on Hooper fields so
    the baseline series has non-zero SD (z-scoring requires SD > 0).
    """
    import random
    rng = random.Random(rng_seed)
    conn = db.get_db()
    for i in range(days):
        d_iso = (target - _td(days=days - 1 - i)).isoformat()
        if jitter:
            sq = max(1, min(7, sleep_q + rng.choice([-1, 0, 1])))
            ft = max(1, min(7, fatigue + rng.choice([-1, 0, 1])))
            so = max(1, min(7, soreness + rng.choice([-1, 0, 1])))
            st = max(1, min(7, stress + rng.choice([-1, 0, 1])))
            mo = max(1, min(7, mood + rng.choice([-1, 0, 1])))
        else:
            sq, ft, so, st, mo = sleep_q, fatigue, soreness, stress, mood
        hooper = sq + ft + st + so
        conn.execute(
            "INSERT OR REPLACE INTO daily_log "
            "(date, sleep_quality, fatigue, soreness, stress, mood, hooper_index) "
            "VALUES (?,?,?,?,?,?,?)",
            (d_iso, sq, ft, so, st, mo, hooper),
        )
    conn.commit()


# ── Tests ───────────────────────────────────────────────────────────────────

def test_insufficient_data_returns_none(fresh_db):
    """1. < 30 days of wellness data → score=None, status='insufficient_data'."""
    today = _date(2026, 5, 5)
    _seed_wellness(today, days=10)
    result = rc.compute_readiness_composite("default", today.isoformat())
    assert result["score"] is None
    assert result["status"] == "insufficient_data"
    assert "Need" in result["advice"] and "30 days" in result["advice"]


def test_static_weights_30_to_59_days(fresh_db):
    """2. 30-59 days → score with static_weights, no Bayesian update fires."""
    today = _date(2026, 5, 5)
    _seed_wellness(today, days=45)
    _seed_daily_log(today, days=45)
    result = rc.compute_readiness_composite("default", today.isoformat())
    assert result["status"] == "static_weights"
    assert isinstance(result["score"], (int, float))
    # weights must equal the locked initial set
    assert result["weights"] == rc.W_INITIAL
    # No persisted weights row should exist (Bayesian update did NOT fire).
    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM athlete_metrics WHERE metric = 'readiness_weights'"
    ).fetchone()
    assert row is None, "Bayesian update fired with < 60 days of data"


def test_dynamic_weights_60_plus_days_diverge(fresh_db):
    """3. ≥ 60 days + 4 weeks of differential signal → weights diverge."""
    today = _date(2026, 5, 5)
    # 70 days of wellness with HRV correlated against eFTP via spike pattern
    conn = db.get_db()
    import random
    rng = random.Random(7)
    for i in range(70):
        d_iso = (today - _td(days=70 - 1 - i)).isoformat()
        # HRV spikes track eFTP rises (synthetic positive correlation).
        hrv = 50.0 + 5.0 * math.sin(i / 4.0) + rng.uniform(-1.0, 1.0)
        eftp = 240.0 + 10.0 * math.sin(i / 4.0) + rng.uniform(-1.0, 1.0)
        conn.execute(
            "INSERT OR REPLACE INTO wellness "
            "(date, ctl, atl, hrv, rhr, sleep_secs, sleep_score, eftp, raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (d_iso, 50.0, 45.0, hrv, 50, 27000, 85, eftp, "{}"),
        )
    conn.commit()
    _seed_daily_log(today, days=70)
    result = rc.compute_readiness_composite("default", today.isoformat())
    assert result["status"] == "dynamic_weights"
    # Weights must have moved away from initial (at least one component
    # diverged by > 0.01 after the Bayesian update). The exact divergence
    # depends on the rng-seeded synthetic signal, so we just check inequality.
    weights = result["weights"]
    diffs = [abs(weights[k] - rc.W_INITIAL[k]) for k in rc.W_INITIAL]
    assert max(diffs) > 0.01, f"Bayesian update did not move any weight: {weights}"
    # Weights still sum to 1.0 within float tolerance.
    assert abs(sum(weights.values()) - 1.0) < 1e-3


def test_all_positive_inputs_score_high(fresh_db):
    """4. All-positive z-scored inputs (≥60d) → score in [7, 10]."""
    today = _date(2026, 5, 5)
    # 70 days of low/moderate baseline so today's high readings z-score positively.
    conn = db.get_db()
    for i in range(70):
        d_iso = (today - _td(days=70 - 1 - i)).isoformat()
        # Final 3 days set HIGH HRV + HIGH TSB (low ATL relative to CTL).
        if i >= 67:
            hrv = 90.0
            ctl_v, atl_v = 55.0, 35.0  # tsb=+20 — peaked
        else:
            hrv = 50.0 + (i % 5)  # baseline mean ~52, low SD
            ctl_v, atl_v = 50.0, 50.0
        conn.execute(
            "INSERT OR REPLACE INTO wellness "
            "(date, ctl, atl, hrv, rhr, sleep_secs, sleep_score, eftp, raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (d_iso, ctl_v, atl_v, hrv, 48, 28800, 92, 250.0, "{}"),
        )
    conn.commit()
    # mood=7 (high) → feel ≈ +1.5; soreness/fatigue/stress low (Hooper low).
    _seed_daily_log(today, days=70, sleep_q=2, fatigue=2, soreness=2,
                     stress=2, mood=7)
    # Today: mood=7, fatigue=2, etc. → Hooper z negative → inverted = positive.
    conn = db.get_db()
    conn.execute(
        "INSERT OR REPLACE INTO daily_log "
        "(date, sleep_quality, fatigue, soreness, stress, mood, hooper_index) "
        "VALUES (?,?,?,?,?,?,?)",
        (today.isoformat(), 1, 1, 1, 1, 7, 4),
    )
    conn.commit()
    # Provide a strong-positive yesterday α1 (≥1.0):
    yesterday = (today - _td(days=1)).isoformat()
    db.log_metric(yesterday, "dfa_alpha1_avg", 1.05, source="test")
    result = rc.compute_readiness_composite("default", today.isoformat())
    assert result["score"] is not None
    assert 7.0 <= result["score"] <= 10.0, (
        f"all-positive inputs scored {result['score']}, expected [7, 10]: "
        f"components={result['components']}"
    )


def test_all_depressed_inputs_score_low(fresh_db):
    """5. All-depressed inputs → score in [0, 3]."""
    today = _date(2026, 5, 5)
    conn = db.get_db()
    for i in range(70):
        d_iso = (today - _td(days=70 - 1 - i)).isoformat()
        if i >= 67:
            hrv = 25.0      # very low compared to baseline
            ctl_v, atl_v = 40.0, 70.0  # tsb=-30 — heavily fatigued
        else:
            hrv = 60.0 + (i % 3)
            ctl_v, atl_v = 50.0, 50.0
        conn.execute(
            "INSERT OR REPLACE INTO wellness "
            "(date, ctl, atl, hrv, rhr, sleep_secs, sleep_score, eftp, raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (d_iso, ctl_v, atl_v, hrv, 60, 18000, 50, 240.0, "{}"),
        )
    conn.commit()
    _seed_daily_log(today, days=70, sleep_q=4, fatigue=4, soreness=4,
                     stress=4, mood=4)
    # today: maxed-out Hooper, low mood
    conn.execute(
        "INSERT OR REPLACE INTO daily_log "
        "(date, sleep_quality, fatigue, soreness, stress, mood, hooper_index) "
        "VALUES (?,?,?,?,?,?,?)",
        (today.isoformat(), 7, 7, 7, 7, 1, 28),
    )
    conn.commit()
    yesterday = (today - _td(days=1)).isoformat()
    db.log_metric(yesterday, "dfa_alpha1_avg", 0.40, source="test")
    result = rc.compute_readiness_composite("default", today.isoformat())
    assert result["score"] is not None
    assert 0.0 <= result["score"] <= 3.0, (
        f"all-depressed inputs scored {result['score']}, expected [0, 3]: "
        f"components={result['components']}"
    )


def test_missing_dfa_alpha1_renormalises(fresh_db):
    """6. Missing dfa_alpha1_y → re-normalisation works, confidence reduced."""
    today = _date(2026, 5, 5)
    # 70 days of standard wellness; do NOT log any dfa_alpha1_avg row.
    _seed_wellness(today, days=70, hrv_base=60.0, hrv_jitter=2.0)
    _seed_daily_log(today, days=70)
    result = rc.compute_readiness_composite("default", today.isoformat())
    # Score should still be computed (we have hrv, tsb, ln_rmssd, hooper, feel)
    assert result["score"] is not None
    # dfa_alpha1_y component should be None
    assert result["components"]["dfa_alpha1_y"] is None
    # confidence < 1.0 because dfa weight (0.15) is unavailable
    assert result["confidence"] < 1.0
    assert result["confidence"] >= 0.5  # still above the floor
    # The dfa_alpha1_y weight is 0.15 by initial; expected available = 0.85
    expected_max = 1.0 - rc.W_INITIAL["dfa_alpha1_y"]
    assert abs(result["confidence"] - expected_max) < 0.01, (
        f"confidence={result['confidence']}, expected≈{expected_max}"
    )


def test_hrv4training_csv_upload(client):
    """7. HRV4Training CSV upload: 7 days of valid rows → wellness.hrv;
        rows missing date or rmssd are skipped (PATCH G16)."""
    csv_text = (
        "date,rmssd,hrv_baseline,recovery_points\n"
        "2026-04-29,55.0,53.0,7\n"
        "2026-04-30,58.0,54.0,8\n"
        "2026-05-01,60.5,55.0,8\n"
        "2026-05-02,62.0,56.0,9\n"
        "2026-05-03,57.0,56.0,7\n"
        "2026-05-04,53.0,55.0,6\n"
        "2026-05-05,49.0,54.0,5\n"
        ",51.0,53.0,5\n"            # missing date — skipped
        "2026-04-28,,53.0,5\n"      # missing rmssd — skipped
    )
    files = {"file": ("hrv4t.csv", csv_text, "text/csv")}
    resp = client.post("/api/wellness/import-hrv4training", files=files)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["imported"] == 7
    assert payload["skipped"] == 2
    # Verify the rows landed in wellness.hrv
    conn = db.get_db()
    rows = conn.execute(
        "SELECT date, hrv FROM wellness WHERE date >= '2026-04-29' "
        "AND date <= '2026-05-05' ORDER BY date"
    ).fetchall()
    assert len(rows) == 7
    hrv_values = [r["hrv"] for r in rows]
    assert hrv_values == [55.0, 58.0, 60.5, 62.0, 57.0, 53.0, 49.0]
