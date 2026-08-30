"""HRR (Karvonen) zone model — issue #10.

Scope is deliberate and documented (docs/SCIENCE.md "Heart-rate zones: LTHR
vs heart-rate reserve"): HRR drives the ZONE TABLE and displays. Workout
prescription stays LTHR-anchored — no validated %FTP→%HRR mapping exists,
and Z5+ keeps RPE per the standing contract. The comparative evidence
(Wolpern 2015, PMID 26146564) favors threshold anchoring when LTHR is known,
so LTHR stays the default and nothing ever auto-switches.

The gates pinned here all came from the adversarial review of the plan:
 * a MEASURED max HR is required — pm.max_hr silently falls back to Tanaka
   208−0.7×age, fine for display, not for a model whose top band is 90-100%
   of it;
 * eligibility is a sample-count rule (≥4 RHR samples in 14 days), not a
   calendar cutoff — one stale reading from three weeks back must not
   enable the model while the rolling window sits empty;
 * ineligibility falls back to LTHR loudly (reason in the payload), never
   silently ignoring the rider's choice.
"""
from __future__ import annotations

import datetime

import pytest

import app as app_module
import zones as zones_mod


def test_hrr_bands_from_reserve():
    z = zones_mod.hrr_zones(45, 190)
    assert [x.low for x in z][1:] == [133, 147, 162, 177]
    assert z[-1].high == 190
    # gap-free and ascending
    for a, b in zip(z, z[1:]):
        assert b.low == a.high + 1


def test_hrr_zones_validate_anchors():
    with pytest.raises(ValueError):
        zones_mod.hrr_zones(0, 190)
    with pytest.raises(ValueError):
        zones_mod.hrr_zones(190, 190)


class _PM:
    def __init__(self, athlete):
        self._athlete = athlete


def _wellness(days_values):
    today = datetime.date.today()
    return [{"id": (today - datetime.timedelta(days=d)).isoformat(),
             "restingHR": v} for d, v in days_values]


def test_model_needs_measured_max_hr(monkeypatch):
    pm = _PM({"hr_zone_model": "hrr"})   # no max_hr key at all
    model, anchor, why = app_module._hr_zone_model(pm)
    assert model == "lthr" and "max HR" in why


def test_sample_count_rule_not_calendar(monkeypatch):
    """One 20-day-old reading used to satisfy a 30-day cutoff. It must not."""
    pm = _PM({"hr_zone_model": "hrr", "max_hr": 190})
    monkeypatch.setattr("ride_storage.load_recent_wellness",
                        lambda days=14: _wellness([(20, 44)]))
    model, anchor, why = app_module._hr_zone_model(pm)
    assert model == "lthr" and "4 resting-HR samples" in why


def test_rolling_median_anchor(monkeypatch):
    pm = _PM({"hr_zone_model": "hrr", "max_hr": 190,
              "hrr_rhr_window_days": 7})
    monkeypatch.setattr("ride_storage.load_recent_wellness",
                        lambda days=14: _wellness(
                            [(0, 46), (1, 44), (2, 45), (3, 52), (5, 43)]))
    model, anchor, why = app_module._hr_zone_model(pm)
    assert model == "hrr"
    assert anchor == 45, (
        "median, not mean — the 52 illness spike must not drag the anchor")


def test_manual_fixed_value_wins(monkeypatch):
    pm = _PM({"hr_zone_model": "hrr", "max_hr": 190,
              "hrr_rhr_mode": "manual", "rhr_baseline": 48})
    monkeypatch.setattr("ride_storage.load_recent_wellness",
                        lambda days=14: _wellness([(0, 60)] * 10))
    model, anchor, why = app_module._hr_zone_model(pm)
    assert (model, anchor) == ("hrr", 48)


def test_lthr_choice_is_untouched():
    pm = _PM({"max_hr": 190})
    assert app_module._hr_zone_model(pm)[0] == "lthr"


def test_zone_table_switches_with_the_model(monkeypatch):
    pm = _PM({"hr_zone_model": "hrr", "max_hr": 190,
              "hrr_rhr_mode": "manual", "rhr_baseline": 45})
    monkeypatch.setattr(app_module, "_custom_zones", lambda kind: None)
    rows = app_module._hr_zones(170, 190, pm=pm)
    assert rows[1]["low"] == 133 and rows[-1]["high"] == 190
    # And without eligibility the SAME call serves LTHR zones.
    pm2 = _PM({"hr_zone_model": "hrr"})
    rows2 = app_module._hr_zones(170, 190, pm=pm2)
    assert rows2 != rows
