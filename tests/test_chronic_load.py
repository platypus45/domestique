"""The load the rider carries: ride_storage.chronic_weekly_tss.

The chronic side of the ACWR, a 28-day EWMA of daily TSS, on the convention
the ramp and the auditor already share (plan_invariants.chronic_after).

It replaced recent_mean_weekly_tss as the planner's answer to "what does this
rider carry?" for two reasons, each with a test below:
  * that one reads list_rides(), the FIT archive alone. The owner's rides
    arrive from intervals.icu, so it returned None for every plan they have
    ever been given and CTL x 7 answered instead — 228 TSS a week against the
    256 they were really carrying (2026-09-13).
  * it averaged only the weeks that held a ride, so it could not decay through
    a lay-off. The ramp guarded that by flooring the load carried at CTL x 7,
    which cost every fit rider the difference. A day off counts as a zero
    here, so the decay is in the number itself and the floor is gone.
"""
from __future__ import annotations

import datetime as dt

import pytest

import ride_storage as rs

TODAY = dt.date(2026, 9, 13)


def _rides(weeks: int, weekly_tss: float, *, until=TODAY, days_per_week: int = 3,
           gap_days: int = 0) -> list[dict]:
    """``weeks`` of riding at ``weekly_tss``, spread over ``days_per_week``,
    ending ``gap_days`` before ``until`` — the load_all_rides shape (top-level
    ``tss``, which list_rides() does not produce)."""
    last = until - dt.timedelta(days=gap_days)
    out = []
    for w in range(weeks):
        monday = last - dt.timedelta(days=last.weekday() + 7 * w)
        for i in range(days_per_week):
            out.append({"ride_id": f"icu_{w}_{i}", "source": "icu",
                        "started_at": (monday + dt.timedelta(days=i * 2)).isoformat() + "T10:00:00",
                        "tss": weekly_tss / days_per_week})
    return out


@pytest.mark.parametrize("asked_on", [
    dt.date(2026, 9, 13), dt.date(2026, 9, 10), dt.date(2026, 9, 8)])
def test_the_load_carried_is_what_the_rider_has_been_riding(asked_on):
    """Steady riding converges on the weekly load itself, whatever weekday the
    question is asked on: the EWMA is in TSS a week, the units the ramp and
    the auditor multiply. A walk that forgot the x 7 reads a seventh of this;
    one that steps a day at a time reads 278 on the Sunday, because this rider
    rides Monday, Wednesday and Friday."""
    carried = rs.chronic_weekly_tss(_rides(16, 300.0, until=asked_on),
                                    today=asked_on)
    assert 295 <= carried <= 305, (asked_on, carried)


def test_it_reads_the_rides_intervals_icu_sent():
    """The owner's whole archive is ICU records. recent_mean_weekly_tss reads
    the FIT archive alone and answers None for them; this reads what
    load_all_rides merges, so the planner sees a rider at all."""
    icu = _rides(16, 300.0)
    assert rs.recent_mean_weekly_tss() is None            # the FIT archive is empty
    assert rs.chronic_weekly_tss(icu, today=TODAY) > 0


def test_a_lay_off_decays_the_load_carried():
    """Three weeks off leave a rider carrying far less than they did. This is
    what the ramp's CTL x 7 floor stood in for (the Step 5 review, L1): the
    mean over active weeks still read 300 here, because it never saw a week
    without a ride."""
    ridden = _rides(16, 300.0, gap_days=21)
    carried = rs.chronic_weekly_tss(ridden, today=TODAY)
    assert carried < 0.65 * 300, carried
    assert rs.recent_mean_weekly_tss(extra_rides=ridden) > 290   # the old answer


def test_months_off_leaves_the_answer_to_ctl():
    """Nothing ridden in the 28-day window: an EWMA decayed to nothing would
    crush the plan to a few TSS a week. CTL is a 42-day EWMA of the same rides
    and describes a returning rider better, so the caller falls back to it."""
    assert rs.chronic_weekly_tss(_rides(16, 300.0, gap_days=40), today=TODAY) is None


def test_an_archive_too_short_to_have_settled_leaves_it_to_ctl():
    """Seeded at zero, the EWMA needs about eight weeks to be within 2% of the
    load. A fresh install with three weeks of history would read ~25% low and
    hand the rider a ceiling under what they train at."""
    assert rs.chronic_weekly_tss(_rides(3, 300.0), today=TODAY) is None
    assert rs.chronic_weekly_tss(_rides(9, 300.0), today=TODAY) is not None


def test_an_empty_archive_says_so():
    """None, not zero: zero would multiply the ACWR ceiling down to nothing."""
    assert rs.chronic_weekly_tss([], today=TODAY) is None
    assert rs.chronic_weekly_tss([{"started_at": "2026-08-01T10:00:00"}],
                                 today=TODAY) is None


@pytest.mark.parametrize("bad", [
    {"started_at": "", "tss": 50},
    {"started_at": "not-a-date", "tss": 50},
    {"started_at": "2026-08-01T10:00:00", "tss": "fifty"},
])
def test_a_malformed_record_does_not_take_the_archive_down(bad):
    """One unreadable record must not cost the rider the other sixteen weeks."""
    carried = rs.chronic_weekly_tss(_rides(16, 300.0) + [bad], today=TODAY)
    assert carried and 285 <= carried <= 315, carried
