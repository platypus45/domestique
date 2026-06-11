"""v1.8.24 — auto plan-update tier-dispatch (_apply_plan_update).

The shared core behind the manual "Update plan" button AND the ride-sync auto
path. Tier-dispatches:
  • current significant absence  → regenerate_from_today (recovery ramp)
  • otherwise                    → reforecast_dict (structure-preserving)

Guards under test (per /tmp/MASTER_DECISIONS_tasks12 §11):
  • RECENT-gap gate — an OLD recovered gap must NOT trigger a rebuild.
  • EPISODE latch — at most one rebuild per absence episode (no churn).
  • EVENT-TAPER guard — never silently rebuild inside the taper window.
  • Event target_date never moves; top-level plan keys survive a rebuild.
"""
from datetime import date, timedelta

import pytest

import app


T = date.today()


def _mkweek(wn, start, tss,
            types=("z2", "z2", "rest", "threshold", "z2", "z2", "rest")):
    sessions = []
    for i, t in enumerate(types):
        d = start + timedelta(days=i)
        sessions.append({
            "day": d.isoformat(), "day_name": d.strftime("%a"),
            "session_type": t, "duration_min": 0 if t == "rest" else 60,
            "tss_estimate": 0 if t == "rest" else 50, "status": "pending",
            "description": "",
        })
    return {
        "week_num": wn, "start": start.isoformat(),
        "end": (start + timedelta(days=6)).isoformat(), "phase": "build",
        "tss_target": tss, "is_stepback": False, "sessions": sessions,
    }


def _base_plan(weeks, goal=None):
    return {
        "goal": goal or {
            "type": "general", "hours_per_week": 8, "max_weekday_hours": 2.0,
            "max_weekend_hours": 3.5, "rest_days": [2, 6], "longest_ride_h_90d": 2.0,
        },
        "availability": {}, "weeks": weeks,
    }


def _past_starts():
    """3 completed week-starts + the current week start (ends in the future)."""
    return (T - timedelta(days=21), T - timedelta(days=14),
            T - timedelta(days=7), T)


def _apply(plan, *, ctl, tsb, activities, debounce=True, today=T):
    return app._apply_plan_update(
        plan, training={"ctl": ctl, "tsb": tsb}, activities=activities,
        today=today, allow_regen=True, gap_debounce=debounce,
    )


def _full_adherence_activities(starts):
    acts = []
    for ws in starts:
        for i in range(7):
            acts.append({"date": (ws + timedelta(days=i)).isoformat(), "tss": 45})
    return acts


# ── tier dispatch ────────────────────────────────────────────────────────────

def test_trained_on_plan_rebalances_not_rebuilds():
    w1, w2, w3, w4 = _past_starts()
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300),
                       _mkweek(5, T + timedelta(days=7), 300)])
    acts = _full_adherence_activities([w1, w2, w3])
    _pd, action, _info, status = _apply(plan, ctl=50, tsb=-5, activities=acts)
    assert action == "rebalanced", status


def test_missed_two_plus_weeks_rebuilds_with_recovery_ramp():
    w1, w2, w3, w4 = _past_starts()
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300),
                       _mkweek(5, T + timedelta(days=7), 300),
                       _mkweek(6, T + timedelta(days=14), 300)])
    pd, action, info, status = _apply(plan, ctl=30, tsb=10, activities=[])
    assert action == "rebuilt", status
    assert info["regen_info"].get("recovery_ramp_weeks", 0) > 0
    assert "recovery ramp" in status.lower()


# ── episode latch (no churn) ─────────────────────────────────────────────────

def test_episode_latch_prevents_double_rebuild():
    w1, w2, w3, w4 = _past_starts()
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300),
                       _mkweek(5, T + timedelta(days=7), 300)])
    pd1, a1, _i, _s = _apply(plan, ctl=30, tsb=10, activities=[])
    assert a1 == "rebuilt"
    assert pd1.get("gap_regen_latch", {}).get("key")
    # Second pass over the just-rebuilt plan (same absence) must NOT rebuild.
    pd2, a2, _i2, _s2 = _apply(pd1, ctl=30, tsb=10, activities=[], debounce=True)
    assert a2 == "rebalanced", "latch failed — rebuilt twice for one absence"


def test_manual_path_also_latched_but_rebuild_from_scratch_is_the_force():
    """gap_debounce=False (if a caller ever wants it) still acts; the default
    manual endpoint uses debounce=True so re-clicks don't reshuffle. Verify the
    debounce flag is what gates the second rebuild."""
    w1, w2, w3, w4 = _past_starts()
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300)])
    pd1, a1, _i, _s = _apply(plan, ctl=30, tsb=10, activities=[])
    assert a1 == "rebuilt"
    # debounce OFF → acts again (force); debounce ON → latched.
    _pd2, a_force, _i2, _s2 = _apply(pd1, ctl=30, tsb=10, activities=[], debounce=False)
    _pd3, a_latched, _i3, _s3 = _apply(pd1, ctl=30, tsb=10, activities=[], debounce=True)
    assert a_force == "rebuilt"
    assert a_latched == "rebalanced"


# ── recent-gap gate (stickiness fix) ─────────────────────────────────────────

def test_old_recovered_gap_does_not_trigger_rebuild():
    """Weeks 1-2 missed but the most-recent completed week (3) trained normally
    → consecutive_missed≥2 yet NO rebuild (gap is historical)."""
    w1, w2, w3, w4 = _past_starts()
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300),
                       _mkweek(5, T + timedelta(days=7), 300)])
    acts = _full_adherence_activities([w3])  # only the recent week trained
    _pd, action, info, _s = _apply(plan, ctl=48, tsb=-3, activities=acts)
    assert info["gaps"]["consecutive_missed"] >= 2  # the old gap is detected …
    assert action == "rebalanced"                   # … but does not rebuild


# ── event-taper guard ────────────────────────────────────────────────────────

def test_latch_band_reflects_current_episode_not_old_longer_gap():
    """Guard 5: an OLDER, longer recovered gap must not poison the latch key.
    Weeks 1-3 missed (old, 3wk), weeks 4-5 trained, weeks 6-7 missed (current,
    2wk). The latch key's band must come from the CURRENT 2-week episode
    (anchored at week 6), not detect_plan_gaps' global 3-week max."""
    # week 1 = T-49 … week 7 = T-7 (last completed), week 8 = T (current week).
    base = T - timedelta(days=49)
    weeks = [_mkweek(n, base + timedelta(days=7 * (n - 1)), 300) for n in range(1, 9)]
    plan = _base_plan(weeks)
    # only weeks 4 (T-28) & 5 (T-21) trained → old gap weeks 1-3, current gap weeks 6-7
    acts = _full_adherence_activities([base + timedelta(days=7 * 3),
                                       base + timedelta(days=7 * 4)])
    pd, action, info, _s = _apply(plan, ctl=35, tsb=8, activities=acts)
    assert action == "rebuilt"
    key = pd.get("gap_regen_latch", {}).get("key", "")
    assert key.startswith("6:"), f"latch anchored at wrong week: {key}"
    assert key.endswith(":s"), f"band should be 's' (2-week episode), got: {key}"


def test_event_taper_window_flags_behind_not_silent_rebuild():
    w1, w2, w3, w4 = _past_starts()
    goal = {"type": "event", "event_date": (T + timedelta(days=10)).isoformat(),
            "hours_per_week": 8, "max_weekday_hours": 2.0,
            "max_weekend_hours": 3.5, "rest_days": [2, 6], "longest_ride_h_90d": 2.0}
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300)], goal=goal)
    pd, action, _info, status = _apply(plan, ctl=30, tsb=10, activities=[])
    assert action == "rebalanced"  # did NOT silently rebuild the taper
    assert pd.get("last_update_info", {}).get("behind_plan") is True
    assert "event" in status.lower()


def test_event_outside_taper_window_rebuilds():
    """Same missed gap but the event is far out (>21d) → normal rebuild."""
    w1, w2, w3, w4 = _past_starts()
    goal = {"type": "event", "event_date": (T + timedelta(days=90)).isoformat(),
            "hours_per_week": 8, "max_weekday_hours": 2.0,
            "max_weekend_hours": 3.5, "rest_days": [2, 6], "longest_ride_h_90d": 2.0}
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300),
                       _mkweek(5, T + timedelta(days=7), 300)], goal=goal)
    pd, action, _info, _s = _apply(plan, ctl=30, tsb=10, activities=[])
    assert action == "rebuilt"
    # event date is immovable
    assert pd["goal"]["event_date"] == goal["event_date"]


# ── invariants on rebuild ────────────────────────────────────────────────────

def test_rebuild_preserves_top_level_keys_and_past_weeks():
    w1, w2, w3, w4 = _past_starts()
    plan = _base_plan([_mkweek(1, w1, 300), _mkweek(2, w2, 300),
                       _mkweek(3, w3, 300), _mkweek(4, w4, 300)])
    plan["availability"] = {w1.isoformat(): {"hours": 1.5, "type": "available"}}
    plan["some_user_key"] = "keepme"
    pd, action, _info, _s = _apply(plan, ctl=30, tsb=10, activities=[])
    assert action == "rebuilt"
    # top-level keys survive (v1.8.20 shallow-copy contract)
    assert pd.get("some_user_key") == "keepme"
    assert "availability" in pd
    # past weeks kept verbatim (start dates still present in the rebuilt plan)
    starts = {w["start"] for w in pd["weeks"]}
    assert w1.isoformat() in starts and w2.isoformat() in starts
