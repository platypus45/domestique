"""Gap detection sees the current week — pro-rata, and never alone.

detect_plan_gaps broke out of its loop at the first week whose end >= today,
so the week being ridden contributed nothing: a rider dark since last Monday
could not complete the 2-week streak the regen tier requires until the NEXT
Monday, days after the pattern was plain. The current week now counts
planned-TSS-to-date against actual, from day 4, at the substantially-missed
tier only.

The guard worth pinning hardest: a dark current week ON ITS OWN must never
trigger a rebuild. The regen gate needs a streak of 2, so a fully missed past
week has to anchor it — the pro-rata week finishes a streak early, it does
not start one. And the recovery ramp is sized from days that actually
elapsed, not from days that have not happened yet.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

import training_planner as tp


def _weeks(n, start_monday, tss=300):
    out = []
    for i in range(n):
        s = start_monday + datetime.timedelta(days=7 * i)
        out.append(tp.PlannedWeek(
            week_num=i + 1, start=s, end=s + datetime.timedelta(days=6),
            phase="build1", tss_target=tss, is_stepback=False, sessions=[]))
    return out


def _acts(day_tss_pairs):
    return [{"date": d, "tss": t} for d, t in day_tss_pairs]


def _run(weeks, acts, today, ctl=40.0):
    class _D(datetime.date):
        @classmethod
        def today(cls):
            return today
    with patch.object(tp, "date", _D):
        return tp.detect_plan_gaps(weeks, acts, ctl)


_MON1 = datetime.date(2026, 8, 3)   # week 1
_MON2 = datetime.date(2026, 8, 10)  # week 2 (current in most tests)


def test_dark_current_week_completes_the_streak_early():
    """Week 1 fully missed, week 2 dark through Friday → regen on Friday,
    not next Monday."""
    weeks = _weeks(2, _MON1)
    g = _run(weeks, [], today=_MON2 + datetime.timedelta(days=4))
    assert g["consecutive_missed"] == 2
    assert g["needs_regeneration"] is True
    partial = [w for w in g["gap_weeks"] if w.get("partial_week")]
    assert len(partial) == 1 and partial[0]["week_num"] == 2


def test_dark_current_week_alone_never_regenerates():
    weeks = _weeks(2, _MON1)
    acts = _acts([((_MON1 + datetime.timedelta(days=i)).isoformat(), 45)
                  for i in range(6)])  # week 1 ridden normally (270 of 300)
    g = _run(weeks, acts, today=_MON2 + datetime.timedelta(days=5))
    assert g["consecutive_missed"] == 1
    assert g["needs_regeneration"] is False


def test_young_current_week_does_not_count():
    """Wednesday of a dark week is 2 elapsed days — noise, not a pattern."""
    weeks = _weeks(2, _MON1)
    g = _run(weeks, [], today=_MON2 + datetime.timedelta(days=2))
    assert all(not w.get("partial_week") for w in g["gap_weeks"])


def test_ridden_current_week_does_not_count():
    weeks = _weeks(2, _MON1)
    acts = _acts([((_MON2 + datetime.timedelta(days=i)).isoformat(), 60)
                  for i in range(4)])  # 240 TSS by Friday
    g = _run(weeks, acts, today=_MON2 + datetime.timedelta(days=4))
    assert all(not w.get("partial_week") for w in g["gap_weeks"])


def test_absence_days_count_only_elapsed_days():
    """Week 1 missed (7d) + current week dark through Friday (4d) = 11, not
    14 — the recovery ramp must not be sized for days that have not happened."""
    weeks = _weeks(2, _MON1)
    g = _run(weeks, [], today=_MON2 + datetime.timedelta(days=4))
    assert g["absence_days"] == 11


def test_past_weeks_behaviour_unchanged():
    """Two fully missed past weeks still regen exactly as before."""
    weeks = _weeks(3, _MON1)
    today = _MON1 + datetime.timedelta(days=15)  # Tue of week 3
    g = _run(weeks, [], today=today)
    assert g["consecutive_missed"] >= 2
    assert g["needs_regeneration"] is True
