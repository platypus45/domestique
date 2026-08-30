"""The programme summary must count the rides the rider actually has.

Two ways it showed zeros over real riding, both reported from live use:

  * It read ride_storage.list_rides(), which globs ride_*.json — FIT imports
    only. A rider whose rides all arrive by intervals.icu sync (the
    rides/icu/i*.json cache) counted zero rides, so the recap showed zeros
    over a window containing a 213-TSS ride.
  * On a CONTINUOUS plan the window began at the last regeneration — possibly
    yesterday — so the recap summarised a day-old window forever. A rolling
    plan has no end to recap; it gets a trailing 28-day window instead.

And one shape trap: load_all_rides mixes FIT records (numbers nested under
"summary") with ICU records (flat). The aggregates read the summary shape, so
flat records get one synthesized — without it the ride COUNT was right while
every total summed zero, which is worse than an empty panel because it looks
like a measured nothing.
"""
from __future__ import annotations

import datetime

import app as app_mod


def _icu_ride(day: str, tss: float, km: float, dur_s: int, kj: float) -> dict:
    """A record shaped like ride_storage.load_all_rides' ICU entries: FLAT."""
    return {
        "ride_id": f"icu_{day}", "source": "icu", "sport": "Ride",
        "started_at": f"{day}T10:00:00", "duration_s": dur_s,
        "distance_km": km, "elevation_m": 100.0, "tss": tss, "kj": kj,
        "decoupling_pct": 3.0,
    }


def _plan(goal_type: str, start: datetime.date, weeks: int = 4) -> dict:
    ws = []
    for i in range(weeks):
        s = start + datetime.timedelta(days=7 * i)
        ws.append({"week_num": i + 1, "start": s.isoformat(),
                   "end": (s + datetime.timedelta(days=6)).isoformat(),
                   "phase": "continuous" if goal_type == "continuous" else "base",
                   "tss_target": 200, "sessions": []})
    return {"goal": {"goal_type": goal_type}, "weeks": ws}


def test_icu_synced_rides_are_counted(monkeypatch):
    today = datetime.date.today()
    plan = _plan("event", today - datetime.timedelta(days=7))
    rides = [_icu_ride((today - datetime.timedelta(days=2)).isoformat(),
                       tss=213, km=124.0, dur_s=14100, kj=2336)]
    import ride_storage
    monkeypatch.setattr(ride_storage, "load_all_rides", lambda: rides)
    s = app_mod._build_programme_summary(plan)
    assert s["rides"] == 1, "an ICU-synced ride was not counted"
    assert s["totals"]["km"] == 124, (
        f"ride counted but km summed {s['totals']['km']} — the flat ICU "
        f"record shape is not reaching the aggregates")
    assert s["totals"]["kj"] == 2336
    assert round(s["totals"]["hours"], 1) == 3.9


def test_continuous_plan_uses_a_trailing_window(monkeypatch):
    """A continuous plan regenerated yesterday must still see last week."""
    today = datetime.date.today()
    plan = _plan("continuous", today - datetime.timedelta(days=1))
    old_ride_day = (today - datetime.timedelta(days=6)).isoformat()
    rides = [_icu_ride(old_ride_day, tss=213, km=124.0, dur_s=14100, kj=2336)]
    import ride_storage
    monkeypatch.setattr(ride_storage, "load_all_rides", lambda: rides)
    s = app_mod._build_programme_summary(plan)
    assert s["start_date"] <= old_ride_day, (
        "the window still starts at plan regeneration — a ride six days old "
        "is invisible and the recap reads zero forever")
    assert s["rides"] == 1


def test_event_plan_window_is_untouched(monkeypatch):
    """The trailing window is a continuous-mode accommodation only: an event
    plan's recap is genuinely about the plan, so its window must stay put."""
    today = datetime.date.today()
    start = today + datetime.timedelta(days=3)          # future plan
    plan = _plan("event", start)
    import ride_storage
    monkeypatch.setattr(ride_storage, "load_all_rides", lambda: [])
    s = app_mod._build_programme_summary(plan)
    assert s["start_date"] == start.isoformat()


def test_empty_plan_reports_zero_rides():
    s = app_mod._build_programme_summary({"weeks": []})
    assert s["weeks"] == 0
    assert s["rides"] == 0, (
        "the modal keys its empty state off 'rides' — absent means the "
        "no-plan case renders as measured zeros again")


def test_fit_records_keep_their_own_summary(monkeypatch):
    """The shim must never overwrite a FIT record's real summary block."""
    today = datetime.date.today()
    plan = _plan("event", today - datetime.timedelta(days=7))
    fit = {"ride_id": "fit_x", "source": "fit",
           "started_at": f"{(today - datetime.timedelta(days=2)).isoformat()}T09:00:00",
           "summary": {"tss": 80, "distance_km": 40.0, "duration_sec": 5400,
                       "kj_mechanical": 900, "elevation_gain_m": 200}}
    import ride_storage
    monkeypatch.setattr(ride_storage, "load_all_rides", lambda: [fit])
    s = app_mod._build_programme_summary(plan)
    assert s["totals"]["km"] == 40
    assert s["totals"]["kj"] == 900
