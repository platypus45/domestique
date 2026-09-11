"""Reforecast's answer to fatigue: ease the coming week, and undo it after.

Production hands reforecast today's TSB for every future day. The loop eased
one rung down the ladder (a threshold day stayed hard, a sprint became VO2max
work, the TSS barely moved), eased every hard session in the plan on one
reading, and kept no record of what it replaced -- so it either eased again on
every sync or, once guarded, never again: a TSB -26 on day 3 left a real -60
crash on day 24 with nothing to ease (the Step 4 review, H1).

Fatigue now eases a hard session in the coming week to a session the athlete
can recover on (_ease_for_recovery), keeps the original, and restores it when
the reading clears. ATL, the fatigue term, is a 7-day average (Banister): today's
reading describes the coming week, not the plan.
"""
from dataclasses import fields
from datetime import date, timedelta

import pytest

import training_planner as tp

MONDAY = date(2026, 9, 14)
_TODAY = [MONDAY]
# Read with defaults so this file runs against the loop it replaced, and fails there.
HORIZON = getattr(tp, "TSB_EASE_HORIZON_DAYS", 7)
EASE_FIELDS = ("session_type", "duration_min", "tss_estimate", "description",
               "zwo_file", "zwo_name")


class _Today(date):
    @classmethod
    def today(cls):
        d = _TODAY[0]
        return cls(d.year, d.month, d.day)


@pytest.fixture(autouse=True)
def _frozen(monkeypatch):
    _TODAY[0] = MONDAY
    monkeypatch.setattr(tp, "date", _Today)


def _stored(seed_salt=4):
    goal = tp.Goal(goal_type="event", event_type="granfondo", event_km=160, event_climb_m=2000,
                   target_date=MONDAY + timedelta(weeks=16, days=6), hours_per_week=10.0,
                   max_weekday_hours=2.0, max_weekend_hours=4.0,
                   available_days=[1, 2, 3, 4, 5, 6], rest_days=[0], plan_weeks=0)
    weeks = tp.generate_plan(goal, seed_salt=seed_salt, current_ctl=55.0, recent_weekly_tss=380.0,
                             athlete={"ftp": 240, "weight_kg": 72})[1]
    names = [f.name for f in fields(tp.PlannedSession)]
    return {"goal": {"type": "event", "event_date": goal.target_date.isoformat()},
            "weeks": [{"week_num": w.week_num, "start": w.start.isoformat(),
                       "end": w.end.isoformat(), "phase": w.phase, "tss_target": w.tss_target,
                       "is_stepback": w.is_stepback,
                       "sessions": [{**{k: getattr(s, k) for k in names},
                                     "day": s.day.isoformat()} for s in w.sessions]}
                      for w in weeks]}


def _sessions(plan):
    return {s["day"]: s for w in plan["weeks"] for s in w["sessions"]}


def _hard(s):
    return s["session_type"] in tp._HARD_SESSION_TYPES


def _sync(plan, day, tsb, **kw):
    """What the app does on a ride sync: today's TSB, for every future day."""
    _TODAY[0] = day
    tp.reforecast_dict(plan, tsb_series={day + timedelta(days=i): float(tsb) for i in range(160)},
                       **kw)


def _window(plan, day, lo, hi):
    return {d: s for d, s in _sessions(plan).items()
            if day + timedelta(days=lo) <= date.fromisoformat(d) <= day + timedelta(days=hi)}


def test_one_reading_eases_the_coming_week_not_the_plan():
    plan = _stored()
    day = MONDAY + timedelta(days=2)
    before = {d: dict(s) for d, s in _sessions(plan).items()}
    _sync(plan, day, -40)
    after = _sessions(plan)
    near = [d for d, s in _window(plan, day, 1, HORIZON).items() if _hard(before[d])]
    far = [d for d in before if date.fromisoformat(d) > day + timedelta(days=HORIZON)]
    assert near, "no hard session in the coming week, so this proves nothing"
    assert all(after[d].get("tsb_eased_from") for d in near)
    assert all(after[d]["session_type"] == before[d]["session_type"] for d in far), \
        "today's reading eased sessions beyond the coming week"


def test_fatigue_eases_to_a_session_the_athlete_can_recover_on():
    plan = _stored()
    day = MONDAY + timedelta(days=2)
    before = {d: dict(s) for d, s in _sessions(plan).items()}
    _sync(plan, day, -40)
    eased = [s for s in _sessions(plan).values() if s.get("tsb_eased_from")]
    assert eased
    for s in eased:
        was = before[s["day"]]
        assert not _hard(s), f"{was['session_type']} eased to {s['session_type']}: still hard"
        assert s["tss_estimate"] < was["tss_estimate"]
        assert s["zwo_file"], "an eased day was left without a workout"
    assert tp._ease_for_recovery("sprint") not in tp._HARD_SESSION_TYPES


def test_the_same_reading_on_every_sync_changes_nothing_more():
    plan = _stored()
    day = MONDAY + timedelta(days=2)
    _sync(plan, day, -40)
    once = {d: dict(s) for d, s in _sessions(plan).items()}
    _sync(plan, day, -40)
    _sync(plan, day, -40)
    assert {d: dict(s) for d, s in _sessions(plan).items()} == once


def test_the_plan_comes_back_when_the_reading_clears():
    plan = _stored()
    day = MONDAY + timedelta(days=2)
    before = {d: dict(s) for d, s in _sessions(plan).items()}
    _sync(plan, day, -40)
    eased = [d for d, s in _sessions(plan).items() if s.get("tsb_eased_from")]
    assert eased
    _sync(plan, day, -5)
    after = _sessions(plan)
    for d in eased:
        for k in EASE_FIELDS:
            assert after[d][k] == before[d][k], (d, k, before[d][k], after[d][k])
        assert not after[d].get("adapted") and "tsb_eased_from" not in after[d]


def test_a_crash_three_weeks_after_a_mild_reading_still_eases():
    """The review's case: TSB -26 on day 3, then a real crash on day 24."""
    plan = _stored()
    base = {d: dict(s) for d, s in _sessions(plan).items()}
    _sync(plan, MONDAY + timedelta(days=3), -26)
    crash = MONDAY + timedelta(days=24)
    ahead = [d for d, s in _window(plan, crash, 1, HORIZON).items() if _hard(base[d])]
    assert ahead, "no hard session in the week after the crash"
    _sync(plan, crash, -60)
    after = _sessions(plan)
    assert all(after[d].get("tsb_eased_from") for d in ahead), \
        f"a -60 crash eased {sum(1 for d in ahead if after[d].get('tsb_eased_from'))} of {len(ahead)}"


def test_no_reading_is_not_a_recovered_reading(monkeypatch):
    """ICU down: get_today_metrics returns {} and the app passes no series. The
    loop read that as 'not fatigued' and restored every eased session (the
    Step 5 review, M1); the next sync with a reading eased them again."""
    plan = _stored()
    day = MONDAY + timedelta(days=2)
    _sync(plan, day, -40)
    eased = {d: dict(s) for d, s in _sessions(plan).items() if s.get("tsb_eased_from")}
    assert eased
    monkeypatch.setattr(tp, "get_today_metrics", lambda: {})
    tp.reforecast_dict(plan, tsb_series=None)
    tp.reforecast_dict(plan, tsb_series={})
    after = _sessions(plan)
    assert {d: after[d] for d in eased} == eased


# A breach that arms G3: 20 % of the time above threshold against a 10 % target.
_BREACH = {"actual_polarization": {"z1_pct": 62, "z2_pct": 18, "z3_pct": 20},
           "target_polarization": {"z1_pct": 80, "z2_pct": 10, "z3_pct": 10}}


def test_a_restore_keeps_what_the_polarization_gate_did():
    """The reading clears in the sync where G3 lowers the restored day. The
    write-back cleared `adapted` after copying G3's, so the next sync under the
    same breach lowered the day again: two tiers for one breach (the Step 5
    review, M3; the ratchet of DUP-22)."""
    plan = _stored()
    day = MONDAY + timedelta(days=2)
    _sync(plan, day, -40)
    eased = [d for d, s in _sessions(plan).items() if s.get("tsb_eased_from")]
    _sync(plan, day, -5, **_BREACH)
    once = {d: dict(s) for d, s in _sessions(plan).items()}
    lowered = [d for d in eased if once[d]["description"].startswith("G3")]
    assert lowered, "G3 lowered no restored day, so this proves nothing"
    assert all(once[d].get("adapted") for d in lowered), "G3's adaptation was erased"
    _sync(plan, day, -5, **_BREACH)
    again = _sessions(plan)
    assert all(again[d] == once[d] for d in lowered), \
        {d: (once[d]["session_type"], again[d]["session_type"]) for d in lowered}


def test_a_restore_keeps_hard_days_48h_apart():
    """The rider drags an eased day next to a hard one. The move carries the
    whole session, its record of the original included, and the restore put
    the hard original back there: 8 of 18 such moves in the Step 5 review (M4).
    Spacing ranks above the session count (D1), so the day stays eased."""
    import copy
    from app import _apply_move_session
    plan = _stored(seed_salt=0)          # seed 4 eases only a tempo day next to a hard one
    day = MONDAY + timedelta(days=1)
    _sync(plan, day, -40)
    S = _sessions(plan)

    def hard_pairs(p):
        hs = sorted(date.fromisoformat(d) for d, s in _sessions(p).items()
                    if date.fromisoformat(d) >= day and tp._session_is_hit(s))
        return {(a, b) for a, b in zip(hs, hs[1:]) if (b - a).days < 2}

    moves = []
    for d, s in sorted(S.items()):
        if not s.get("tsb_eased_from"):
            continue
        dd = date.fromisoformat(d)
        for k in range(7):
            dst = dd - timedelta(days=dd.weekday() - k)
            nb = [(dst + timedelta(days=j)).isoformat() for j in (-1, 1)]
            if (dst > day and dst != dd and dst.isoformat() in S
                    and not tp._session_is_hit(S[dst.isoformat()])
                    and any(x != d and tp._session_is_hit(S.get(x)) for x in nb)):
                moves.append((d, dst.isoformat()))
                break
    assert moves, "no eased day can be moved next to a hard one"
    for src, dst in moves:
        p = copy.deepcopy(plan)
        assert _apply_move_session(p, src, dst)
        before = hard_pairs(p)
        _sync(p, day, -5)
        assert not hard_pairs(p) - before, f"{src} moved to {dst} came back hard beside another"
