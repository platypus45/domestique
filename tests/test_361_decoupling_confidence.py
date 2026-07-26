"""v3.6.0 — decoupling confidence, attribution, and the missing-value copy.

Three related changes, all display-layer. Established before touching any of
them: `decoupling_advisory` is never read by `training_planner.py`, so nothing
here can move a session. That is what makes widening the window safe.

C1 — the old rule dropped a decoupling reading after 2 days. One rest day was
     enough to silence it, which is exactly when a fatigue signal is worth
     seeing. The v1.8.16 bug being patched was a stale reading presenting
     itself as current; labelling fixes that without hiding the signal.
C6 — poor sleep/stress raise cardiac drift at a fixed workload (Temesi 2013
     PMID 23760468; Kong 2025 SMD 0.39), so an elevated signal plus a poor
     wellness rating are usually one cause read twice. Attribute, don't add.
C4' — measured on the live archive: every ride with no decoupling number had
     no POWER. "Not enough valid samples" pointed the rider at a data bug that
     did not exist.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import readiness as R

_DASH = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"


# ── C1: graded recency ──────────────────────────────────────────────────────

@pytest.mark.parametrize("age,expected", [
    (0, "fresh"), (1, "fresh"), (3, "fresh"),
    (4, "aging"), (7, "aging"), (10, "aging"),
    (11, "stale"), (40, "stale"),
])
def test_confidence_bands(age, expected):
    out = R.check_aerobic_decoupling(9.9, source_age_days=age)
    assert out["confidence"] == expected
    assert out["advisory"] is (expected != "stale")


def test_a_single_rest_day_no_longer_silences_the_signal():
    """The concrete regression: 3 days off used to erase the reading."""
    out = R.check_aerobic_decoupling(9.9, source_age_days=3)
    assert out["advisory"] is True


def test_age_travels_with_an_aging_advisory():
    """A days-old number must never be phrased as this morning's."""
    out = R.check_aerobic_decoupling(9.9, source_age_days=8)
    assert "8d ago" in out["reason"]
    assert "Recent ride" not in out["reason"]


def test_unknown_age_is_labelled_unknown_not_silently_fresh():
    out = R.check_aerobic_decoupling(9.9, source_age_days=None, tsb=-2.0)
    assert out["advisory"] is True
    assert out["confidence"] == "unknown"


def test_veto_applies_to_fresh_only():
    kw = dict(tsb=17.0, readiness_status="GOOD", dfa_present_and_healthy=True)
    assert R.check_aerobic_decoupling(9.9, source_age_days=1, **kw)["advisory"] is False
    assert R.check_aerobic_decoupling(9.9, source_age_days=5, **kw)["advisory"] is True


def test_below_threshold_never_advises_regardless_of_age():
    for age in (0, 5, 9):
        assert R.check_aerobic_decoupling(3.0, source_age_days=age)["advisory"] is False


# ── C6: attribution, not addition ───────────────────────────────────────────

def test_attribution_is_none_when_nothing_fired():
    assert R.attribute_fatigue_signal(False, 10.0) == "none"
    assert R.attribute_fatigue_signal(False, None) == "none"


def test_poor_wellness_explains_the_signal():
    assert R.attribute_fatigue_signal(True, 25.0) == "explained"


def test_normal_wellness_leaves_it_unexplained():
    """The case worth surfacing: the body drifts while the rider feels fine."""
    assert R.attribute_fatigue_signal(True, 75.0) == "unexplained"


def test_no_wellness_log_is_not_silently_called_explained():
    """`daily_log` was empty for 180 days on the live profile — an absent
    rating must not be read as 'the rider told us why'."""
    assert R.attribute_fatigue_signal(True, None) == "unattributed"


def test_attribution_rides_along_on_the_composite_payload():
    r = R.compute_readiness(
        ln_rmssd_7d=3.2, swc_lower=3.0, swc_upper=3.4,
        tsb=-8.0, sleep_h=5.2, rhr_delta=4.0, subjective=2.0,
        last_decoupling_pct=9.9, last_decoupling_age_days=1,
    )
    dec = r["decoupling_advisory"]
    assert dec["advisory"] is True
    assert dec["attribution"] == "explained"   # subjective 2/10 → poor
    assert dec["confidence"] == "fresh"


def test_attribution_flags_the_mismatch_case_end_to_end():
    r = R.compute_readiness(
        ln_rmssd_7d=3.2, swc_lower=3.0, swc_upper=3.4,
        tsb=-8.0, sleep_h=8.0, rhr_delta=-1.0, subjective=9.0,
        last_decoupling_pct=9.9, last_decoupling_age_days=6,
    )
    dec = r["decoupling_advisory"]
    assert dec["attribution"] == "unexplained"
    assert dec["confidence"] == "aging"


def test_attribution_never_moves_the_score():
    """Labelling only — the composite must be identical with and without it."""
    kw = dict(ln_rmssd_7d=3.2, swc_lower=3.0, swc_upper=3.4, tsb=-8.0,
              sleep_h=5.2, rhr_delta=4.0, subjective=2.0)
    with_sig = R.compute_readiness(last_decoupling_pct=9.9,
                                   last_decoupling_age_days=1, **kw)
    without = R.compute_readiness(**kw)
    assert with_sig["score"] == without["score"]
    assert with_sig["status"] == without["status"]


# ── UI contract ─────────────────────────────────────────────────────────────

def test_banner_hedges_an_aging_reading_and_shows_attribution():
    src = _DASH.read_text(encoding="utf-8")
    assert "lower confidence" in src
    assert "decoupling_advisory_detail || {}).attribution" in src
    assert "unexplained" in src


def test_missing_decoupling_names_missing_power_not_missing_samples():
    src = _DASH.read_text(encoding="utf-8")
    assert "No power recorded for this ride" in src
    # The old catch-all must survive for the case it is actually true for.
    assert "Not enough valid samples" in src
