"""One codec between a persisted plan and its objects, and it loses nothing.

There were seven hand-typed week readers, two session readers that disagreed on
12 fields, and a generate endpoint that wrote 11 of a session's 27. Reforecast's
reader dropped `adapted`, `completion_matches`, `moved_from` and `execution`; a
rider's CTL target was never persisted; every rebuild deleted the readiness undo
stash (notes/review/dupes.md DUP-5, DUP-22, DUP-25). The same session was also
downgraded again on every sync: the reader was half of that, and the TSB loop,
which never checked `adapted`, the other half.

The two dict-path tests write the stored plan by field name, not through the
codec, so they run against the old readers too -- and fail there.
"""
import json
from dataclasses import fields
from datetime import date, timedelta

import pytest

import training_planner as tp

TODAY = date(2026, 9, 14)                   # a Monday; the planner reads "today"


class _Today(date):
    @classmethod
    def today(cls):
        return cls(TODAY.year, TODAY.month, TODAY.day)


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch):
    monkeypatch.setattr(tp, "date", _Today)


def _plan():
    goal = tp.Goal(goal_type="event", event_type="granfondo", event_km=160,
                   event_climb_m=2000, target_date=TODAY + timedelta(weeks=12),
                   hours_per_week=10.0, max_weekday_hours=2.0, max_weekend_hours=4.0,
                   available_days=[1, 2, 3, 4, 5, 6], rest_days=[0], plan_weeks=12,
                   events=[tp.TargetEvent(date=TODAY + timedelta(weeks=6),
                                          priority="B", name="Kermis", event_type="crit",
                                          event_km=80, event_climb_m=300)])
    _p, weeks = tp.generate_plan(goal, seed_salt=5, current_ctl=50.0,
                                 recent_weekly_tss=400.0, athlete={"ftp": 240, "weight_kg": 72})
    return goal, weeks


def _stored(weeks):
    """The weeks as the app's full writer stored them, by field name."""
    names = [f.name for f in fields(tp.PlannedSession)]
    return [{"week_num": w.week_num, "start": w.start.isoformat(), "end": w.end.isoformat(),
             "phase": w.phase, "tss_target": w.tss_target, "is_stepback": w.is_stepback,
             "sessions": [{**{k: getattr(s, k) for k in names}, "day": s.day.isoformat()}
                          for s in w.sessions]} for w in weeks]


def test_a_plan_round_trips_unchanged_and_serialises():
    _g, weeks = _plan()
    once = [tp.week_to_dict(w) for w in weeks]
    json.dumps(once)                                        # what the plan file needs
    twice = [tp.week_to_dict(tp.week_from_dict(d)) for d in once]
    assert twice == once
    assert any(s.get("is_race") for d in once for s in d["sessions"]), "fixture has a race"


def test_every_field_is_written():
    _g, weeks = _plan()
    d = tp.week_to_dict(weeks[0])
    assert set(tp._WEEK_FIELDS) <= set(d)
    assert set(tp._SESSION_FIELDS) <= set(d["sessions"][0])


def test_session_state_survives_and_response_decoration_does_not():
    """The readiness undo stash (pre_adapt) is state no field holds, and every
    rebuild deleted it. The keys the response enricher writes into the dict it
    serves are not state: a rebuild drops them, as it always did."""
    d = {"day": "2026-09-15", "session_type": "vo2max", "duration_min": 60,
         "tss_estimate": 80, "description": "x",
         "pre_adapt": {"session_type": "sprint", "duration_min": 45},
         "card_state": "planned", "zone_dist": {"z2": 50}}
    out = tp.session_to_dict(tp.session_from_dict(d))
    assert out["pre_adapt"] == d["pre_adapt"]
    assert "card_state" not in out and "zone_dist" not in out


def test_the_reforecast_reader_keeps_what_the_athlete_owns():
    """DUP-22: reforecast_dict reads through _plan_dict_to_planned_weeks, which
    dropped `adapted`, `completion_matches`, `moved_from` and `execution`."""
    _g, weeks = _plan()
    plan = {"weeks": _stored(weeks)}
    s = next(x for x in plan["weeks"][1]["sessions"] if x["session_type"] != "rest")
    s.update(adapted=True, completion_matches=[{"activity_id": "a1"}],
             moved_from="2026-09-10", execution={"score": 88})
    back = {x.day.isoformat(): x for w in tp._plan_dict_to_planned_weeks(plan) for x in w.sessions}
    got = back[s["day"]]
    assert got.adapted is True
    assert got.completion_matches == [{"activity_id": "a1"}]
    assert got.moved_from == "2026-09-10" and got.execution == {"score": 88}


def test_reforecast_adapts_a_session_once_not_on_every_sync():
    """The same projected TSB, synced three times, must not keep downgrading
    the same session: nothing about the athlete changed between the syncs. The
    review blamed the reader, which dropped `adapted`. Carrying it is needed,
    but the TSB loop never checked it: without that guard this failed the same
    way on both readers (vo2max -> threshold -> overunder, one tier a sync)."""
    goal, weeks = _plan()
    plan = {"goal": {"type": "event", "event_date": goal.target_date.isoformat()},
            "weeks": _stored(weeks)}
    tsb = {TODAY + timedelta(days=i): -40.0 for i in range(120)}
    seen = []
    for _ in range(3):
        tp.reforecast_dict(plan, tsb_series=tsb)
        seen.append({s["day"]: s["session_type"] for w in plan["weeks"] for s in w["sessions"]})
    assert any(s.get("adapted") for w in plan["weeks"] for s in w["sessions"]), \
        "the TSB crash adapted nothing, so this test proves nothing"
    moved = {d: [v[d] for v in seen] for d in seen[0] if len({v[d] for v in seen}) > 1}
    assert not moved, f"downgraded again on a later sync: {moved}"


def test_the_goal_block_keeps_its_names_and_its_targets():
    goal, _w = _plan()
    goal.target_ctl, goal.vo2_microintervals_only = 72.0, True
    d = tp.goal_to_dict(goal)
    for legacy in ("type", "event_date", "event_climb", "daily_max_hours", "events"):
        assert legacy in d, legacy                         # the dashboard reads these
    back = tp.goal_from_dict(json.loads(json.dumps(d)))
    assert back.target_ctl == 72.0 and back.vo2_microintervals_only is True
    assert back.events[0].name == "Kermis" and back.target_date == goal.target_date
    assert tp.goal_to_dict(back) == d


def test_a_goal_block_from_before_the_keys_existed_reads_as_it_was_planned():
    old = {"type": "event", "event_date": "2027-01-10", "rest_days": [0],
           "hours_per_week": 9}
    g = tp.goal_from_dict(old)
    assert g.distribution == "polarized"                    # pre-J1 plans were polarized
    assert g.available_days == [1, 2, 3, 4, 5, 6] and g.target_ctl is None
