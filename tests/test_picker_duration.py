"""Workout Picker duration honesty (forum bug, 2026-08-30).

A 30-minute request used to return 60-minute-plus workouts: the flat ±30
window spanned [0, 60] and score-ordering favours high-TSS (long) files.
The requested duration is now a HARD CAP — nothing longer is ever served
(owner decision) — the window flexes downward only, and results are ordered
by closeness to the requested time.
"""
from __future__ import annotations


def test_picker_30min_returns_short_workouts():
    import app
    d = app.api_picker(subjective=7, duration=30)
    ws = d["workouts"]
    assert ws, "picker returned no workouts at 30min"
    for w in ws:
        assert (w.get("Duration(min)") or 0) <= 30, (
            f"30-min request served {w.get('Duration(min)')}min: "
            f"{w.get('Name')}")


def test_picker_orders_by_duration_closeness():
    import app
    d = app.api_picker(subjective=7, duration=60)
    ws = d["workouts"]
    assert ws
    diffs = [abs((w.get("Duration(min)") or 0) - 60) for w in ws]
    assert diffs == sorted(diffs)


def test_picker_75min_default_still_works():
    import app
    d = app.api_picker(subjective=7, duration=75)
    assert d["workouts"]


def test_picker_never_exceeds_hard_cap_any_duration():
    import app
    for dur in (30, 45, 90, 180):
        d = app.api_picker(subjective=7, duration=dur)
        for w in d["workouts"]:
            assert (w.get("Duration(min)") or 0) <= dur, (
                f"{dur}-min request served {w.get('Duration(min)')}min")
