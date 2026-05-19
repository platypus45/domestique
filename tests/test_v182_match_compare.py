"""v1.8.2 MATCH-IMPL — compare_plan_to_actual helper contract.

Pins the locked 6-field shape and exercises each `match_status` branch:
matched / matched_extended / matched_truncated / different_workout /
missed / no_plan. Foster (1998) 25 %-overshoot threshold and 0.7/0.5
cosine bands are decision constants — these tests fail loudly if anyone
relaxes them.

Also pins the back-compat contract on ``app._summarize_ride_for_calendar``:
callers that don't pass ``planned_session=`` still receive the legacy
payload shape with no ``compare`` key.
"""
from __future__ import annotations

import app as app_module
from analytics import compare_plan_to_actual


# ── Locked 6-field shape ────────────────────────────────────────────────────

_LOCKED_FIELDS = frozenset({
    "match_status",
    "tss_delta_pct",
    "duration_delta_min",
    "zone_distribution_match",
    "intent_match",
    "reasons",
})


def _assert_shape(result: dict) -> None:
    assert set(result.keys()) == _LOCKED_FIELDS, (
        f"compare_plan_to_actual returned unexpected keys: "
        f"{set(result.keys()) ^ _LOCKED_FIELDS}"
    )
    assert isinstance(result["match_status"], str)
    assert result["tss_delta_pct"] is None or isinstance(result["tss_delta_pct"], (int, float))
    assert isinstance(result["duration_delta_min"], int)
    assert isinstance(result["zone_distribution_match"], (int, float))
    assert isinstance(result["intent_match"], (int, float))
    assert isinstance(result["reasons"], list)
    assert 0.0 <= result["zone_distribution_match"] <= 1.0
    assert 0.0 <= result["intent_match"] <= 1.0


# ── Status-branch coverage ──────────────────────────────────────────────────

def test_matched_exact():
    """Planned and actual line up on TSS, duration, and zone shape → matched."""
    planned = {
        "session_type": "threshold",
        "tss_estimate": 80,
        "duration_min": 60,
        "zone_dist": {"z1": 20, "z2": 30, "z3": 25, "z4": 20, "z5": 5, "z6": 0},
    }
    actual = {
        "tss": 82,
        "duration_min": 62.0,
        "z1z2_min": 31.0,
        "z3z4_min": 28.0,
        "z5plus_min": 3.0,
    }
    result = compare_plan_to_actual(planned, actual)
    _assert_shape(result)
    assert result["match_status"] == "matched"
    assert abs(result["tss_delta_pct"]) < 10
    assert abs(result["duration_delta_min"]) <= 15
    assert result["zone_distribution_match"] >= 0.7


def test_matched_extended_vo2_block_tacked_on():
    """Planned 73-min neuromuscular + tacked-on VO2 block: +35 % TSS, +12 min,
    same zone family → matched_extended (Foster 1998 spike, but intent held)."""
    planned = {
        "session_type": "neuromuscular",
        "tss_estimate": 76,
        "duration_min": 73,
        "zone_dist": {"z1": 35, "z2": 0, "z3": 27, "z4": 27, "z5": 1, "z6": 0},
    }
    actual = {
        "tss": 103,             # +35 %
        "duration_min": 90.0,   # +17 min
        "z1z2_min": 30.0,       # ≈ same low-end share
        "z3z4_min": 40.0,
        "z5plus_min": 20.0,     # extra high-end block
    }
    result = compare_plan_to_actual(planned, actual)
    _assert_shape(result)
    assert result["match_status"] == "matched_extended"
    assert result["tss_delta_pct"] > 25
    assert result["duration_delta_min"] > 0


def test_matched_truncated_bail_out():
    """Planned 73-min threshold ride, rider bails at ~40 min: -45 % TSS,
    -33 min, but zone shape still close → matched_truncated."""
    planned = {
        "session_type": "threshold",
        "tss_estimate": 80,
        "duration_min": 73,
        "zone_dist": {"z1": 20, "z2": 25, "z3": 25, "z4": 25, "z5": 5, "z6": 0},
    }
    actual = {
        "tss": 44,
        "duration_min": 40.0,
        "z1z2_min": 18.0,
        "z3z4_min": 20.0,
        "z5plus_min": 2.0,
    }
    result = compare_plan_to_actual(planned, actual)
    _assert_shape(result)
    assert result["match_status"] == "matched_truncated"
    assert result["tss_delta_pct"] < -25
    assert result["duration_delta_min"] < 0


def test_different_workout_planned_hit_did_z2():
    """Planned VO2 (z5-heavy), actual is all aerobic z1/z2 → zdm < 0.5 →
    different_workout. Avoid false-positive: actual TSS happens to land
    near planned but the SHAPE disagrees."""
    planned = {
        "session_type": "vo2max",
        "tss_estimate": 70,
        "duration_min": 60,
        "zone_dist": {"z1": 25, "z2": 5, "z3": 5, "z4": 5, "z5": 50, "z6": 10},
    }
    actual = {
        "tss": 65,
        "duration_min": 60.0,
        "z1z2_min": 60.0,   # all aerobic
        "z3z4_min": 0.0,
        "z5plus_min": 0.0,
    }
    result = compare_plan_to_actual(planned, actual)
    _assert_shape(result)
    assert result["match_status"] == "different_workout"
    assert result["zone_distribution_match"] < 0.5


def test_missed_planned_session_no_ride():
    """Planned session, no ride logged → missed (deltas = None / 0)."""
    planned = {
        "session_type": "endurance",
        "tss_estimate": 60,
        "duration_min": 75,
        "zone_dist": {"z1": 60, "z2": 30, "z3": 10, "z4": 0, "z5": 0, "z6": 0},
    }
    result = compare_plan_to_actual(planned, None)
    _assert_shape(result)
    assert result["match_status"] == "missed"
    assert result["tss_delta_pct"] is None
    assert result["duration_delta_min"] == 0


def test_no_plan_rest_day_with_spontaneous_ride():
    """Planned rest day but rider rode anyway → no_plan (the ride wasn't the
    plan; v1.8.2 design rules)."""
    planned = {"session_type": "rest", "tss_estimate": 0, "duration_min": 0}
    actual = {
        "tss": 55,
        "duration_min": 60.0,
        "z1z2_min": 50.0,
        "z3z4_min": 10.0,
        "z5plus_min": 0.0,
    }
    result = compare_plan_to_actual(planned, actual)
    _assert_shape(result)
    assert result["match_status"] == "no_plan"


def test_no_plan_unplanned_ride_planned_none():
    """No planned session at all (planned=None) → no_plan."""
    actual = {
        "tss": 30,
        "duration_min": 45.0,
        "z1z2_min": 40.0,
        "z3z4_min": 5.0,
        "z5plus_min": 0.0,
    }
    result = compare_plan_to_actual(None, actual)
    _assert_shape(result)
    assert result["match_status"] == "no_plan"
    assert result["tss_delta_pct"] is None


def test_missing_zone_dist_falls_back_to_tss_only():
    """Planned session has no zone_dist (unknown session_type, no library
    row) and actual has no zone info either → zdm = 0.0, helper degrades to
    TSS-only and returns 'matched' (never false-positive 'different_workout'
    on missing instrumentation)."""
    planned = {
        "session_type": "custom_unknown",
        "tss_estimate": 70,
        "duration_min": 60,
        # zone_dist intentionally absent
    }
    actual = {
        "tss": 68,
        "duration_min": 58.0,
        # no z1z2_min / z3z4_min / z5plus_min
    }
    result = compare_plan_to_actual(planned, actual)
    _assert_shape(result)
    assert result["match_status"] == "matched"
    assert result["zone_distribution_match"] == 0.0


# ── Edge / contract coverage ────────────────────────────────────────────────

def test_intent_match_dominant_bucket_share():
    """intent_match reflects fraction of planned dominant bucket delivered.
    Planned 50 % z5+, actual 25 % z5+ → intent_match ≈ 0.5."""
    planned = {
        "session_type": "vo2max",
        "tss_estimate": 70,
        "duration_min": 60,
        "zone_dist": {"z1": 25, "z2": 5, "z3": 10, "z4": 10, "z5": 40, "z6": 10},
    }
    actual = {
        "tss": 60,
        "duration_min": 60.0,
        "z1z2_min": 30.0,
        "z3z4_min": 15.0,
        "z5plus_min": 15.0,
    }
    result = compare_plan_to_actual(planned, actual)
    _assert_shape(result)
    # Dominant planned bucket is z5plus (50 %); actual delivers 25 %.
    assert 0.4 <= result["intent_match"] <= 0.6


def test_summarize_back_compat_no_planned_kwarg():
    """Existing callers of _summarize_ride_for_calendar that don't pass
    planned_session= still get the legacy payload (no `compare` key).
    Critical: actual_secondary list and any other legacy caller paths."""
    ride = {
        "ride_id": "icu_999",
        "source": "icu",
        "name": "Endurance",
        "duration_s": 3600,
        "tss": 50,
        "avg_power_w": 180,
        "time_in_zone": {"z1": 1800, "z2": 1500, "z3": 300, "z4": 0,
                         "z5": 0, "z6": 0, "z7": 0},
        "started_at": "2026-05-19T07:00:00Z",
    }
    summary = app_module._summarize_ride_for_calendar(ride, ftp=250)
    assert "compare" not in summary
    # Locked baseline payload fields still present.
    assert summary["ride_id"] == "icu_999"
    assert summary["tss"] == 50


def test_summarize_attaches_compare_when_planned_session_supplied():
    """When the calendar-render path passes planned_session=, the returned
    payload gains a `compare` block with the locked 6 fields."""
    ride = {
        "ride_id": "icu_777",
        "source": "icu",
        "name": "Threshold",
        "duration_s": 3600,  # 60 min
        "tss": 82,
        "avg_power_w": 220,
        "time_in_zone": {"z1": 600, "z2": 900, "z3": 900, "z4": 900,
                         "z5": 300, "z6": 0, "z7": 0},
        "started_at": "2026-05-19T07:00:00Z",
    }
    planned = {
        "session_type": "threshold",
        "tss_estimate": 80,
        "duration_min": 60,
        "zone_dist": {"z1": 25, "z2": 25, "z3": 25, "z4": 20, "z5": 5, "z6": 0},
    }
    summary = app_module._summarize_ride_for_calendar(
        ride, ftp=250, planned_session=planned,
    )
    assert "compare" in summary
    _assert_shape(summary["compare"])
    assert summary["compare"]["match_status"] == "matched"
