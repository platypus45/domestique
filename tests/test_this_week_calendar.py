"""v1.8.25 — "This Week" calendar merge bugs.

Bug 1 (fixed): merge_plan_with_rides only fed the PRIMARY (longest) ride of a
day into the week's actual_tss / zone minutes; secondary rides (a 2nd ride that
day — commute + trainer) were shown but not counted, so the on-track bar +
completion% read falsely "behind". Now ALL rides on a day count.

Bug 2 (fixed, UI): a completed ride with a null TSS rendered a RED "failed" tint
— the server accumulation guards null with `or 0`; this test pins that a
null-TSS ride does not crash the merge and the day still carries an actual.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module  # noqa: E402


def _icu_ride(rid, day_iso, hour, dur_s, tss, tiz):
    return {
        "source": "icu", "ride_id": rid,
        "name": rid, "started_at": f"{day_iso}T{hour:02d}:00:00",
        "duration_s": dur_s, "tss": tss, "avg_power_w": 200,
        "time_in_zone": tiz,
    }


def _plan_with_today():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sessions = []
    for off in range(7):
        d = (monday + timedelta(days=off)).isoformat()
        sessions.append({
            "day": d, "day_name": "X", "session_type": "z2",
            "duration_min": 60, "tss_estimate": 50, "status": "pending",
            "zwo_file": "endurance_steady_65pct_60min.zwo", "zwo_name": "Endurance 60",
            "description": "",
        })
    return {
        "goal": {"type": "general"},
        "weeks": [{
            "week_num": 1, "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "phase": "build", "tss_target": 350, "is_stepback": False,
            "sessions": sessions,
        }],
        "availability": {},
    }


def _current_week(payload):
    return next((w for w in payload["weeks"] if w.get("is_current")), None)


def test_secondary_ride_counts_toward_weekly_actual():
    today = date.today().isoformat()
    plan = _plan_with_today()
    # Two rides TODAY: a 60-min primary (TSS 50) + a 30-min secondary (TSS 30).
    rides = [
        _icu_ride("icu_a", today, 7, 3600, 50, {"z1": 600, "z2": 3000}),
        _icu_ride("icu_b", today, 18, 1800, 30, {"z2": 1800}),
    ]
    payload = app_module.merge_plan_with_rides(plan, rides)
    cur = _current_week(payload)
    assert cur is not None, "current ISO week must exist"
    # actual_tss must include BOTH rides (50 + 30 = 80), not just the primary 50.
    assert abs(cur["actual_tss"] - 80.0) < 0.5, f"actual_tss={cur['actual_tss']} (expected ~80, both rides)"
    # zone minutes likewise sum both: Z1+Z2 = (600+3000)/60 + 1800/60 = 60 + 30 = 90
    assert abs(cur["actual_z1z2_min"] - 90.0) < 0.6, f"z1z2={cur['actual_z1z2_min']} (expected ~90)"
    # the secondary ride is also surfaced on the day cell
    day = next((d for d in cur["days"] if d["date"] == today), None)
    assert day and day.get("actual") is not None
    assert day.get("actual_secondary"), "secondary ride should appear in actual_secondary"


def test_single_ride_unchanged():
    today = date.today().isoformat()
    plan = _plan_with_today()
    rides = [_icu_ride("icu_a", today, 7, 3600, 55, {"z1": 600, "z2": 3000})]
    payload = app_module.merge_plan_with_rides(plan, rides)
    cur = _current_week(payload)
    assert abs(cur["actual_tss"] - 55.0) < 0.5


def test_null_tss_ride_does_not_crash_and_day_has_actual():
    today = date.today().isoformat()
    plan = _plan_with_today()
    # ride with NO tss (None) — must not crash, contributes 0, but the day is
    # still "done" (carries an actual) so the UI can render it neutral, not red.
    rides = [_icu_ride("icu_x", today, 7, 3600, None, {"z1": 600, "z2": 3000})]
    payload = app_module.merge_plan_with_rides(plan, rides)
    cur = _current_week(payload)
    assert cur is not None
    assert cur["actual_tss"] == 0.0  # null TSS contributes 0 (no estimate)
    day = next((d for d in cur["days"] if d["date"] == today), None)
    assert day and day.get("actual") is not None  # ride present → UI tint neutral, not red
