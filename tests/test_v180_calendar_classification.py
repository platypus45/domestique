"""v1.8.0 §F2 — calendar activity coloring backend wiring.

The frontend will color each calendar activity card by its polarization
classification. This test pins the BACKEND contract: the
``_summarize_ride_for_calendar`` helper must pass ``classification`` and
``pol_confidence`` through to the calendar payload (None when absent).
"""
from __future__ import annotations

import app as app_module


def test_summarize_passes_through_classification_for_icu():
    """ICU rides carry polarization at top level; classification/pol_confidence
    surface on the calendar summary."""
    ride = {
        "ride_id": "icu_12345",
        "source": "icu",
        "name": "Threshold day",
        "duration_s": 3600,
        "tss": 90,
        "avg_power_w": 220,
        "time_in_zone": {"z1": 600, "z2": 1200, "z3": 900, "z4": 600,
                         "z5": 300, "z6": 0, "z7": 0},
        "decoupling_pct": 3.5,
        "started_at": "2026-05-19T07:00:00Z",
        "polarization": {
            "z1z2_pct": 50.0, "z3z4_pct": 41.7, "z5plus_pct": 8.3,
            "polarization_index": 0.5,
            "classification": "pyramidal",
            "confidence": 0.85,
        },
    }
    summary = app_module._summarize_ride_for_calendar(ride, ftp=250)
    assert summary["classification"] == "pyramidal"
    assert summary["pol_confidence"] == 0.85


def test_summarize_classification_none_when_no_polarization_icu():
    """ICU ride without a polarization block → classification + pol_confidence None."""
    ride = {
        "ride_id": "icu_67890",
        "source": "icu",
        "name": "Recovery",
        "duration_s": 1800,
        "tss": 25,
        "avg_power_w": 130,
        "time_in_zone": {"z1": 1800},
        "decoupling_pct": None,
        "started_at": "2026-05-19T07:00:00Z",
        # no "polarization" key
    }
    summary = app_module._summarize_ride_for_calendar(ride, ftp=250)
    assert "classification" in summary
    assert summary["classification"] is None
    assert summary["pol_confidence"] is None


def test_summarize_passes_through_classification_for_fit():
    """Non-ICU rides (FIT / JSON) also carry classification + pol_confidence."""
    ride = {
        "ride_id": "fit_abc",
        "source": "fit",
        "summary": {
            "duration_sec": 3600, "tss": 75, "avg_power": 200,
            "decoupling_pct": 2.1, "workout_name": "Z2 ride",
        },
        "started_at": "2026-05-19T07:00:00Z",
        "polarization": {
            "classification": "base",
            "confidence": 0.7,
        },
    }
    summary = app_module._summarize_ride_for_calendar(ride, ftp=250)
    assert summary["classification"] == "base"
    assert summary["pol_confidence"] == 0.7


def test_summarize_classification_none_when_no_polarization_fit():
    """FIT ride without polarization → None passthrough."""
    ride = {
        "ride_id": "fit_xyz",
        "source": "fit",
        "summary": {"duration_sec": 3600, "tss": 75, "avg_power": 200,
                    "workout_name": "Z2"},
        "started_at": "2026-05-19T07:00:00Z",
    }
    summary = app_module._summarize_ride_for_calendar(ride, ftp=250)
    assert summary["classification"] is None
    assert summary["pol_confidence"] is None


def test_summarize_handles_non_dict_polarization():
    """Defensive: legacy ride with polarization=None (not a dict)."""
    ride = {
        "ride_id": "icu_legacy",
        "source": "icu",
        "duration_s": 3600,
        "tss": 60,
        "avg_power_w": 180,
        "time_in_zone": {"z1": 1800, "z2": 1800},
        "polarization": None,
    }
    summary = app_module._summarize_ride_for_calendar(ride, ftp=250)
    assert summary["classification"] is None
    assert summary["pol_confidence"] is None
