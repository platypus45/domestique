"""v3.2.1 — sleep/HRV last-known fallback.

Today's overnight metrics reach intervals.icu hours after wake-up
(Garmin→ICU lag): the day's wellness row exists but hrv/sleepSecs/sleepScore
are null while restingHR lands earlier. Pre-3.2.1 the home tiles showed bare
"—" for those; now compute_sleep_metrics_from_wellness falls back to the
last non-null night and stamps `hrv_asof` / `sleep_asof` so the UI can tag
the value as stale (amber + date) instead of passing it off as today's.
"""
from datetime import date, timedelta

from sleep import compute_sleep_metrics_from_wellness


def _day(offset: int) -> str:
    return (date.today() - timedelta(days=offset)).isoformat()


def test_unsynced_today_falls_back_to_last_known_night():
    wellness = [
        {"id": _day(2), "hrv": 70, "restingHR": 40,
         "sleepSecs": 28800, "sleepScore": 80},
        # today: RHR synced, overnight metrics not yet (the live repro)
        {"id": _day(0), "hrv": None, "restingHR": 38,
         "sleepSecs": None, "sleepScore": None},
    ]
    m = compute_sleep_metrics_from_wellness(wellness)
    assert m["hrv_ms"] == 70
    assert m["hrv_asof"] == _day(2)
    assert m["sleep_h"] == 8.0
    assert m["sleep_score"] == 80
    assert m["sleep_asof"] == _day(2)
    assert m["rhr_today"] == 38  # RHR chain untouched


def test_synced_today_reports_today_no_stale_tag():
    wellness = [
        {"id": _day(2), "hrv": 70, "restingHR": 40,
         "sleepSecs": 28800, "sleepScore": 80},
        {"id": _day(0), "hrv": 75, "restingHR": 38,
         "sleepSecs": 30000, "sleepScore": 85},
    ]
    m = compute_sleep_metrics_from_wellness(wellness)
    assert m["hrv_ms"] == 75
    assert m["hrv_asof"] == _day(0)
    assert m["sleep_asof"] == _day(0)
    assert m["sleep_score"] == 85
