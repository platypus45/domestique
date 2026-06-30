"""v2.4.1 — HRVT aggregate: drop high-decoupling rides + r²-weighted median.

DFA α1 is a fatigue/durability marker (Rogers 2022 PMID 35615679, 2025 PMID
39904800): a high-decoupling ride has a non-stationary α1 so its HRVT is biased
low. The aggregate must exclude such rides and weight the rest by fit confidence
(r²), not treat every qualifying ride as one equal vote.
"""
import datetime as _dt

from fastapi.testclient import TestClient

import app


def _ride(power, r2, decoupling, day_offset):
    day = (_dt.date.today() - _dt.timedelta(days=day_offset)).isoformat()
    return {
        "dfa_alpha1_status": "computed",
        "dfa_algo_version": 99,
        "summary": {"dfa_alpha1_avg": 0.9},
        "started_at": day + "T08:00:00",
        "moving_s": 3600,
        "avg_hr": 150,
        "dfa_hrvt1": {"hr": 150, "power": power, "r2_hr": r2, "r2_power": r2},
        "dfa_hrvt2": None,
        "decoupling_pct": decoupling,
    }


def test_aggregate_excludes_high_decoupling_and_weights_by_r2(monkeypatch):
    rides = [
        _ride(200, 0.90, 2.0, 1),    # high-confidence, well-coupled
        _ride(205, 0.85, 3.0, 2),
        _ride(210, 0.60, 4.0, 3),
        _ride(150, 0.80, 18.0, 4),   # HIGH decoupling → must be excluded
    ]
    monkeypatch.setattr(app, "_iter_icu_dfa_rides", lambda: rides)
    agg = TestClient(app.app).get("/api/profile/dfa-rides").json()["aggregate"]

    # The drifting ride is dropped, and flagged.
    assert agg["n_excluded_decoupling"] == 1
    # Aggregate tracks the well-coupled, high-r² rides — NOT dragged toward the
    # excluded 150 W outlier.
    power = agg["hrvt1"]["power"]
    assert power is not None and 195 <= power <= 212, power


def test_aggregate_keeps_rides_without_decoupling_data(monkeypatch):
    # Missing decoupling_pct must NOT exclude a ride (don't penalise absent data).
    rides = [_ride(200, 0.9, None, 1), _ride(205, 0.9, None, 2), _ride(210, 0.9, None, 3)]
    for r in rides:
        r["decoupling_pct"] = None
    monkeypatch.setattr(app, "_iter_icu_dfa_rides", lambda: rides)
    agg = TestClient(app.app).get("/api/profile/dfa-rides").json()["aggregate"]
    assert agg["n_excluded_decoupling"] == 0
    assert agg["hrvt1"] is not None and agg["hrvt1"]["power"] is not None
