"""v1.4.0 — classify_card_state_v2 declared rules table.

Pure-function classifier per CALENDAR_REDESIGN §5d. 10 states. Each test
asserts the (date, has_actual, session_type, zwo_file, availability_hours)
→ state mapping.

States covered (10):
  past_no_ride, past_planned_no_ride, past_actual_only,
  past_planned_actual, today_planned, today_actual,
  future_planned, future_unavailable, future_rest, missing_workout

Plus the legacy_card_state mapper (10 → 4) for wire back-compat.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402


TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
TOMORROW = (date.today() + timedelta(days=1)).isoformat()


def _mk(day: str, **kw) -> dict:
    """Helper: build a session dict with sensible defaults."""
    return {
        "day": day,
        "day_name": "Mon",
        "session_type": kw.get("session_type", "z2"),
        "duration_min": kw.get("duration_min", 60),
        "tss_estimate": kw.get("tss_estimate", 45),
        "description": "",
        "zwo_file": kw.get("zwo_file", "real.zwo"),
        "zwo_name": kw.get("zwo_name", "Real workout"),
        "status": "pending",
        **{k: v for k, v in kw.items() if k not in {
            "session_type", "duration_min", "tss_estimate", "zwo_file", "zwo_name",
        }},
    }


# ── 10-state classifier ──────────────────────────────────────────────────


def test_past_no_ride():
    """Past + rest day + no ride = past_no_ride (genuine rest)."""
    s = _mk(YESTERDAY, session_type="rest", zwo_file="", duration_min=0)
    assert app_module.classify_card_state_v2(s, has_actual=False, today_iso=TODAY) == "past_no_ride"


def test_past_planned_no_ride():
    """Past + planned + no ride = past_planned_no_ride (skipped)."""
    s = _mk(YESTERDAY)
    assert app_module.classify_card_state_v2(s, has_actual=False, today_iso=TODAY) == "past_planned_no_ride"


def test_past_actual_only():
    """Past + ride + no zwo_file = past_actual_only (unplanned ride)."""
    s = _mk(YESTERDAY, zwo_file="")
    assert app_module.classify_card_state_v2(s, has_actual=True, today_iso=TODAY) == "past_actual_only"


def test_past_planned_actual():
    """Past + ride + zwo_file = past_planned_actual (completed)."""
    s = _mk(YESTERDAY)
    assert app_module.classify_card_state_v2(s, has_actual=True, today_iso=TODAY) == "past_planned_actual"


def test_today_planned():
    """Today + planned + no ride = today_planned."""
    s = _mk(TODAY)
    assert app_module.classify_card_state_v2(s, has_actual=False, today_iso=TODAY) == "today_planned"


def test_today_actual():
    """Today + ride = today_actual (overrides any other state)."""
    s = _mk(TODAY)
    assert app_module.classify_card_state_v2(s, has_actual=True, today_iso=TODAY) == "today_actual"


def test_future_planned():
    """Future + planned + zwo_file = future_planned."""
    s = _mk(TOMORROW)
    assert app_module.classify_card_state_v2(s, has_actual=False, today_iso=TODAY) == "future_planned"


def test_future_unavailable():
    """Future + availability=0 + non-rest = future_unavailable."""
    s = _mk(TOMORROW, availability_hours=0)
    assert app_module.classify_card_state_v2(s, has_actual=False, today_iso=TODAY) == "future_unavailable"


def test_future_rest():
    """Future + session_type=rest = future_rest (rest beats avail=0)."""
    s = _mk(TOMORROW, session_type="rest", zwo_file="", duration_min=0)
    assert app_module.classify_card_state_v2(s, has_actual=False, today_iso=TODAY) == "future_rest"


def test_missing_workout():
    """Future + planned + zwo_file empty = missing_workout (yellow ⚠)."""
    s = _mk(TOMORROW, zwo_file="")
    assert app_module.classify_card_state_v2(s, has_actual=False, today_iso=TODAY) == "missing_workout"


# ── Edge cases per grill ─────────────────────────────────────────────────


def test_rest_beats_unavailable_grill_i6():
    """GRILL I6: rest takes precedence over availability=0 — already a rest
    day so no red badge needed. Visual stays REST.
    """
    s = _mk(TOMORROW, session_type="rest", zwo_file="", duration_min=0,
            availability_hours=0)
    assert app_module.classify_card_state_v2(s, has_actual=False, today_iso=TODAY) == "future_rest"


def test_has_actual_beats_unavailable_grill_i4():
    """GRILL I4: has_actual ALWAYS wins. Even on an unavailable day, a
    logged ride surfaces (rider rode anyway).
    """
    s = _mk(TOMORROW, availability_hours=0)
    state = app_module.classify_card_state_v2(s, has_actual=True, today_iso=TODAY)
    assert state in ("past_actual_only", "today_actual"), (
        f"has_actual must override avail=0; got {state}"
    )


# ── Legacy 4-state mapper (wire back-compat) ─────────────────────────────


@pytest.mark.parametrize("v2_state,expected_legacy", [
    ("today_actual", "completed"),
    ("past_planned_actual", "completed"),
    ("past_actual_only", "completed"),
    ("rest", "rest"),
    ("future_rest", "rest"),
    ("past_no_ride", "rest"),
    ("missing_workout", "missing_workout"),
    ("future_unavailable", "rest"),
    ("past_planned_no_ride", "planned"),
    ("today_planned", "planned"),
    ("future_planned", "planned"),
])
def test_legacy_card_state_map(v2_state, expected_legacy):
    """legacy_card_state(state10) → state4 wire contract."""
    assert app_module.legacy_card_state(v2_state) == expected_legacy
