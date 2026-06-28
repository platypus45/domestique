"""v2.3.0 — custom intensity distribution (user-set % of each training type) +
realized-distribution readout.

The custom split is the HARD-work distribution; easy/Z2 volume, weekly TSS and
HIT count stay at the polarized baseline (parity with pyramidal/threshold). The
realized mix is delivered via a dynamic per-week HIT-type blueprint so what the
user requests actually shows up in the plan.
"""
import tempfile
import pathlib
from collections import Counter

import pytest

import training_planner as tp
import app


# ── budget engine ────────────────────────────────────────────────────────────

def test_custom_budget_reallocates_hard_keeping_easy_tss_total():
    tp.set_active_distribution("custom", {"tempo_ss": 50, "threshold": 30, "vo2": 15, "sprint": 5})
    try:
        cust = tp.get_budget_for_phase("build1")
    finally:
        tp.set_active_distribution("polarized")
    base = tp.get_budget_for_phase("build1")
    # Easy volume, weekly TSS and TOTAL hard minutes are preserved...
    assert cust.z1z2_minutes_per_week == base.z1z2_minutes_per_week
    assert cust.tss_per_week == base.tss_per_week
    cust_hard = cust.z3_minutes_per_week + cust.z4_minutes_per_week + cust.z5plus_minutes_per_week
    base_hard = base.z3_minutes_per_week + base.z4_minutes_per_week + base.z5plus_minutes_per_week
    assert cust_hard == base_hard
    # ...only the SPLIT changes (tempo_ss-led → z3 is the largest hard zone).
    assert cust.z3_minutes_per_week >= cust.z4_minutes_per_week >= cust.z5plus_minutes_per_week


def test_custom_polarized_targets_do_not_crash():
    # get_active_polarized_targets() must handle the custom model (the recalc
    # breach gate calls it) — would KeyError before the fix.
    tp.set_active_distribution("custom", {"tempo_ss": 40, "threshold": 40, "vo2": 20, "sprint": 0})
    try:
        targets = tp.get_active_polarized_targets()
        assert "build1" in targets and "z1z2_pct" in targets["build1"]
    finally:
        tp.set_active_distribution("polarized")


def test_empty_custom_falls_back_to_polarized():
    assert tp.set_active_distribution("custom", {}) == "polarized"
    assert tp.set_active_distribution("custom", None) == "polarized"
    tp.set_active_distribution("polarized")


# ── helpers ──────────────────────────────────────────────────────────────────

def test_custom_hit_sequence_is_proportional():
    seq = tp._custom_hit_sequence({"tempo_ss": 70, "threshold": 20, "vo2": 10, "sprint": 0})
    c = Counter(seq)
    # sweetspot (tempo_ss) dominates; sprint absent.
    assert c["sweetspot"] > c["threshold"] >= c["vo2max"]
    assert c["sprint"] == 0


def test_realized_band_distribution_normalizes():
    plan = {"weeks": [{"sessions": [
        {"session_type": "z2", "duration_min": 100},
        {"session_type": "vo2max", "duration_min": 50},
        {"session_type": "rest", "duration_min": 0},
        {"session_type": "ftp_test", "duration_min": 30},  # excluded
    ]}]}
    d = tp.realized_band_distribution(plan)
    assert d["easy"] == 67 and d["vo2"] == 33
    assert sum(d.values()) in (99, 100, 101)  # rounding


def test_parse_custom_bands_validates():
    assert app._parse_custom_bands({"tempo_ss": 50, "threshold": 50}) == {
        "tempo_ss": 50.0, "threshold": 50.0, "vo2": 0.0, "sprint": 0.0}
    assert app._parse_custom_bands({}) == {}
    assert app._parse_custom_bands("nope") == {}
    assert app._parse_custom_bands({"tempo_ss": -5}) == {}  # all <=0 → empty


def test_preset_typical_bands_present():
    for k in ("polarized", "pyramidal", "threshold"):
        assert set(tp.PRESET_TYPICAL_BANDS[k]) == set(tp.BAND_ORDER)


# ── end-to-end generate (sandboxed plan dir — never touches real ~/.domestique) ─

@pytest.fixture
def sandbox_plan_dir(monkeypatch):
    tmp = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(tp, "PLAN_DIR", tmp / "plans", raising=False)
    yield tmp


def _gen(client, bands):
    body = {"goal_type": "ftp", "hours_per_week": 8, "plan_weeks": 8,
            "available_days": [1, 2, 3, 4, 5, 6], "rest_days": [0],
            "custom_bands": bands}
    r = client.post("/api/plan/generate", json=body)
    assert r.status_code == 200, r.text[:300]
    return r.json()["plan_json"]


def test_generate_custom_persists_and_tracks_request(sandbox_plan_dir):
    from fastapi.testclient import TestClient
    client = TestClient(app.app)

    pj = _gen(client, {"tempo_ss": 70, "threshold": 20, "vo2": 10, "sprint": 0})
    # Persisted + coerced to the custom blueprint path.
    assert pj["goal"]["distribution"] == "custom"
    assert pj["goal"]["custom_bands"]["tempo_ss"] == 70.0
    assert pj["goal"]["plan_mode"] == "template"
    assert pj["goal"]["template_id"] == "custom"
    assert "realized_bands" in pj

    # The HARD work actually tracks the request: sweetspot is the dominant HIT.
    hits = Counter()
    for w in pj["weeks"]:
        for s in w["sessions"]:
            if s["session_type"] in ("sweetspot", "threshold", "vo2max", "sprint"):
                hits[s["session_type"]] += 1
    assert hits["sweetspot"] >= max(hits["threshold"], hits["vo2max"], hits["sprint"])


def test_generate_vo2_heavy_tracks_request(sandbox_plan_dir):
    from fastapi.testclient import TestClient
    client = TestClient(app.app)
    pj = _gen(client, {"tempo_ss": 0, "threshold": 10, "vo2": 70, "sprint": 20})
    hits = Counter()
    for w in pj["weeks"]:
        for s in w["sessions"]:
            if s["session_type"] in ("sweetspot", "threshold", "vo2max", "sprint"):
                hits[s["session_type"]] += 1
    assert hits["vo2max"] >= max(hits["sweetspot"], hits["threshold"])
