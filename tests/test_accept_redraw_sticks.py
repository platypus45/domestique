"""An accepted redraw stays accepted.

The rider previews a workout, clicks Accept, and a different workout lands in
the plan. Mechanism, confirmed on a live plan: accept-redraw installed the
candidate without pinning it, then ran a reforecast over the whole plan. The
reforecast applies the availability calendar literally — a 51-min accepted
file on a 60-min day is a 17% change, over the 15% re-match threshold — so it
re-matched the day while EXCLUDING the file just accepted. Deterministic:
any accepted workout whose length differs >=15% from the day's hours got
replaced on the spot.

Two fixes, both pinned here: accept-redraw marks the day user_swapped (the
same pin swap-type and the FTP-test choice already set), and the reforecast
availability reflow leaves pinned days alone — except a day the rider zeroed,
which still becomes rest whatever was pinned.
"""
from __future__ import annotations

import datetime
import json

import training_planner as tp


def _plan(d, stype, dur, zwo, pinned=False):
    monday = d - datetime.timedelta(days=6)
    return {
        "goal": {"type": "general", "hours_per_week": 5, "distribution": "polarized"},
        "phases": [],
        "availability": {},
        "weeks": [{
            "week_num": 1, "start": monday.isoformat(), "end": d.isoformat(),
            "phase": "build1", "tss_target": 300,
            "sessions": [{
                "day": d.isoformat(), "day_name": d.strftime("%A"),
                "description": stype, "session_type": stype,
                "duration_min": dur,
                "tss_estimate": round(dur / 60 * tp.TSS_PER_HOUR.get(stype, 45)),
                "status": "pending", "zwo_file": zwo,
                "zwo_name": zwo.replace(".zwo", ""), "user_swapped": pinned,
            }],
        }],
    }


def _reforecast(plan, overrides, today):
    plan, _, _ = tp.reforecast_dict(plan, today_iso=today.isoformat(),
                                    availability_overrides=overrides)
    return plan["weeks"][0]["sessions"][0]


def test_pinned_day_keeps_its_workout_under_availability_reflow():
    """51-min accepted file, 60-min day: previously re-matched to a different
    file; now untouched."""
    today = datetime.date(2026, 8, 17)
    d = today + datetime.timedelta(days=6)
    plan = _plan(d, "vo2max", 51, "vo2_short_3x11x30s-15s_120pct_51min.zwo", pinned=True)
    s = _reforecast(plan, {d.isoformat(): 1.0}, today)
    assert s["zwo_file"] == "vo2_short_3x11x30s-15s_120pct_51min.zwo", (
        f"pinned workout was replaced by {s['zwo_file']}")
    assert s["duration_min"] == 51


def test_unpinned_day_still_reflows():
    """The reflow itself is not disabled — an auto-picked day still follows the
    calendar (the v1.7.3 literal-hours behaviour, unchanged)."""
    today = datetime.date(2026, 8, 17)
    d = today + datetime.timedelta(days=6)
    plan = _plan(d, "z2", 45, "endurance_steady_65pct_45min.zwo")
    s = _reforecast(plan, {d.isoformat(): 1.5}, today)
    assert s["duration_min"] == 90


def test_pinned_day_zeroed_by_the_rider_still_becomes_rest():
    today = datetime.date(2026, 8, 17)
    d = today + datetime.timedelta(days=6)
    plan = _plan(d, "vo2max", 51, "vo2_short_3x11x30s-15s_120pct_51min.zwo", pinned=True)
    s = _reforecast(plan, {d.isoformat(): 0.0}, today)
    assert s["session_type"] == "rest"


def test_accept_redraw_pins_and_survives_its_own_reforecast(tmp_path, monkeypatch):
    import app as app_mod
    monday = datetime.date(2026, 8, 17)
    d = monday + datetime.timedelta(days=6)
    plan = {
        "goal": {"type": "general", "hours_per_week": 5, "distribution": "polarized"},
        "phases": [],
        "availability": {d.isoformat(): {"hours": 1.0, "type": "available"}},
        "weeks": [{
            "week_num": 1, "start": monday.isoformat(),
            "end": (monday + datetime.timedelta(days=6)).isoformat(),
            "phase": "build1", "tss_target": 300,
            "sessions": [{
                "day": d.isoformat(), "day_name": d.strftime("%A"),
                "description": "vo2max", "session_type": "vo2max",
                "duration_min": 60, "tss_estimate": 75, "status": "pending",
                "zwo_file": "vo2max_6x3min-3min_110pct_60min.zwo",
                "zwo_name": "vo2max_6x3min-3min_110pct_60min",
            }],
        }],
    }
    monkeypatch.setattr(app_mod.db, "query_activities", lambda **kw: [])
    monkeypatch.setattr(app_mod, "cached", lambda *a, **k: {"ctl": 40, "tsb": 3.0})

    class _D(datetime.date):
        @classmethod
        def today(cls):
            return monday
    monkeypatch.setattr(app_mod, "date", _D)

    cand = {"zwo_file": "vo2_short_3x11x30s-15s_120pct_51min.zwo",
            "zwo_name": "vo2_short_3x11x30s-15s_120pct_51min",
            "variation": 0, "tss_estimate": 62, "duration_min": 51}
    out = app_mod._accept_redraw_apply(plan, d.isoformat(), cand)
    assert out["ok"]
    s = plan["weeks"][0]["sessions"][0]
    assert s["zwo_file"] == cand["zwo_file"], (
        f"accepted {cand['zwo_file']} but the plan holds {s['zwo_file']}")
    assert s["user_swapped"] is True
    assert s["duration_min"] == 51
