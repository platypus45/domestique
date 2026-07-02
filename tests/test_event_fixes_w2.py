"""Wave 2 regression tests for the P4.1 event-planner audit (v2.5.0).

Scope (locked by the grill outcome in IP_EVENT_FIXES.md):
  F4c (D4)  — runway < 14d → race-week-only micro-plan + payload warning
  FC3       — race immutability: _protect_race consulted by every writer
              (E3 parametrized over the tp mutators + the app endpoints)
  FC5a (L3-1)  — auto-move never lands a HARD in T-2..T+0 of an A/B event;
                 auto-moves set auto_moved, never user_moved
  FC3  (L3-2)  — unridden race → terminal missed_race, never rescheduled
  F5b  (L3-4)  — _demote_hit_window demotes only pending, unpinned sessions
  FC5d (L3-6)  — regen keeps §6.12 state in ramp weeks, caps at race+2w,
                 refuses passed targets (app surfaces 400)
  FC3  (L3-7)  — auto-adjust severity=rest never wipes the race day
  FC3  (L3-10) — swap-type / redraw refuse the race day
  L3-11        — dismiss-session operates on ALL sessions matching the date
  FC5c (L3-12) — refit re-owe ≤ 1.0× the missed dose near the race
  FC5d (L3-13) — regen runs the same volume/per-day clamps as generate
  E12          — availability hours=0 on the race date never rests the race

Pinned env (W8 pattern, tests/conftest.py): current_ctl=50.0,
recent_weekly_tss=650.0, frozen today=2026-01-05 (Monday), seed_salt=0.
Own mutable frozen-date class (matrix-module pattern) because the refit/regen
scenarios advance "today" mid-test.
"""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import app as app_module
import training_planner as tp
from conftest import PLANNER_PIN_ANCHOR as ANCHOR, PLANNER_PIN_ARGS

_LIB_INDEX = Path(__file__).resolve().parent.parent / "workouts" / ".library_index.json"

TARGET_16W = ANCHOR + timedelta(days=112)


class _FrozenDate(date):
    _today = ANCHOR

    @classmethod
    def today(cls):
        return cls(cls._today.year, cls._today.month, cls._today.day)


@pytest.fixture(scope="module", autouse=True)
def _pinned_env():
    backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tp, "date", _FrozenDate)
        mp.setattr(tp, "get_today_metrics", lambda: {})
        yield
    _FrozenDate._today = ANCHOR
    if backup is not None and _LIB_INDEX.read_bytes() != backup:
        _LIB_INDEX.write_bytes(backup)


@pytest.fixture(autouse=True)
def _reset_today():
    _FrozenDate._today = ANCHOR
    yield
    _FrozenDate._today = ANCHOR


def _event_goal(target=TARGET_16W, **kw):
    base = dict(goal_type="event", target_date=target, event_name="TestFondo",
                event_km=150.0, event_climb_m=1500.0)
    base.update(kw)
    return tp.Goal(**base)


def _sess(day, st, dur, **kw):
    tss = kw.pop("tss", round(dur / 60 * tp.TSS_PER_HOUR.get(st, 45)))
    s = tp.PlannedSession(
        day=day, day_name=day.strftime("%a"), session_type=st,
        duration_min=dur, tss_estimate=tss, description=st)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _race_fixture(goal=None, stepback_race_week=False):
    """Two hand-built weeks (build2 + taper) with the race MARKED on the target
    date by _mark_race_days itself, so the race meta is byte-consistent with
    what every later pass would recompute (E9 idempotency)."""
    goal = goal or _event_goal()
    target = goal.target_date
    w2_start = target - timedelta(days=6)
    w1_start = w2_start - timedelta(days=7)
    week1 = tp.PlannedWeek(
        week_num=15, start=w1_start, end=w1_start + timedelta(days=6),
        phase="build2", tss_target=400, is_stepback=False,
        sessions=[
            _sess(w1_start, "z2", 90),
            _sess(w1_start + timedelta(days=1), "vo2max", 60),
            _sess(w1_start + timedelta(days=2), "z2", 120),
            _sess(w1_start + timedelta(days=3), "rest", 0),
            _sess(w1_start + timedelta(days=4), "threshold", 60),
            _sess(w1_start + timedelta(days=5), "long_z2", 180),
            _sess(w1_start + timedelta(days=6), "recovery", 45),
        ])
    week2 = tp.PlannedWeek(
        week_num=16, start=w2_start, end=target,
        phase="taper", tss_target=180, is_stepback=stepback_race_week,
        sessions=[
            _sess(w2_start, "z2", 60),
            _sess(w2_start + timedelta(days=1), "vo2max", 45),
            _sess(w2_start + timedelta(days=2), "rest", 0),
            _sess(w2_start + timedelta(days=3), "z2", 45),
            _sess(w2_start + timedelta(days=4), "recovery", 30),
            _sess(w2_start + timedelta(days=5), "z2", 45, is_opener=True),
            _sess(target, "z2", 90),  # becomes the race below
        ])
    weeks = [week1, week2]
    tp._mark_race_days(weeks, goal)
    race = next(s for w in weeks for s in w.sessions if s.day == target)
    assert race.is_race and race.race, "fixture: race not marked"
    return goal, weeks, race


def _snap(s):
    return (s.day, s.session_type, s.duration_min, s.tss_estimate,
            s.description, s.zwo_file, s.zwo_name, s.status,
            bool(getattr(s, "user_moved", False)),
            getattr(s, "dismissed_at", "") or "",
            bool(getattr(s, "is_race", False)),
            getattr(s, "race", None),
            bool(getattr(s, "is_opener", False)))


# ── E3 — race immutability, parametrized over every guarded tp mutator ──────

def _mut_volume_ceiling(goal, weeks):
    tp._enforce_weekly_volume_ceiling(weeks, recent_weekly_tss=650.0, goal=goal)


def _mut_volume_ceiling_taper_only(goal, weeks):
    tp._enforce_weekly_volume_ceiling(weeks, recent_weekly_tss=650.0, goal=goal,
                                      taper_only=True)


def _mut_stepback_lightest(goal, weeks):
    # Make the race week a stepback so the shrink/rest loops actually run.
    weeks[1].is_stepback = True
    weeks[1].phase = "build2"  # the pass skips taper rows; force it live
    tp._enforce_stepback_is_lightest(weeks)


def _mut_demote_hit_window(goal, weeks):
    tp._demote_hit_window(weeks, goal.target_date, 3,
                          tp.load_workout_library())


def _mut_weekly_hit_cap(goal, weeks):
    tp._enforce_weekly_hit_cap(weeks, tp.load_workout_library())


def _mut_build2_peak_floor(goal, weeks):
    lib = tp.load_workout_library()
    tp._enforce_build2_peak_hard_floor(weeks, tp._build_pool_indexes(lib),
                                       {}, {}, {}, {}, set())


def _mut_ronnestad_floor(goal, weeks):
    lib = tp.load_workout_library()
    tp._enforce_ronnestad_floor(weeks, tp._build_pool_indexes(lib), {})


def _mut_long_ride_target(goal, weeks):
    for w in weeks:
        tp._apply_long_ride_target(w.sessions, target_min=300,
                                   max_weekend_min=300, is_stepback=False)


def _mut_easy_slot_content(goal, weeks):
    tp._enforce_easy_slot_content(weeks, tp.load_workout_library(),
                                  weeks[0].start, 0)


def _mut_reforecast_tsb(goal, weeks):
    days = [s.day for w in weeks for s in w.sessions]
    tp.reforecast(goal, weeks, tsb_series={d: -30.0 for d in days})


def _mut_reforecast_avail_zero(goal, weeks):
    # E12 writer #12 — availability rescale with hours=0 on the race date.
    tp.reforecast(goal, weeks, tsb_series={},
                  availability_overrides={goal.target_date.isoformat(): 0.0})


def _mut_refit(goal, weeks):
    _FrozenDate._today = weeks[1].start + timedelta(days=2)
    for s in weeks[1].sessions:
        if s.day < _FrozenDate.today() and s.session_type == "vo2max":
            s.status = "missed"
    tp.refit_remaining_week(goal, weeks, _FrozenDate.today(), seed_salt=7)


_E3_MUTATORS = {
    "volume_ceiling": _mut_volume_ceiling,
    "volume_ceiling_taper_only": _mut_volume_ceiling_taper_only,
    "stepback_lightest": _mut_stepback_lightest,
    "demote_hit_window": _mut_demote_hit_window,
    "weekly_hit_cap": _mut_weekly_hit_cap,
    "build2_peak_hard_floor": _mut_build2_peak_floor,
    "ronnestad_floor": _mut_ronnestad_floor,
    "long_ride_target": _mut_long_ride_target,
    "easy_slot_content": _mut_easy_slot_content,
    "reforecast_tsb": _mut_reforecast_tsb,
    "reforecast_avail_zero": _mut_reforecast_avail_zero,
    "refit_remaining_week": _mut_refit,
}


@pytest.mark.parametrize("name", sorted(_E3_MUTATORS))
def test_e3_race_session_byte_stable_under_mutator(name):
    goal, weeks, race = _race_fixture()
    before = _snap(race)
    _E3_MUTATORS[name](goal, weeks)
    race_after = [s for w in weeks for s in w.sessions
                  if s.day == goal.target_date]
    assert len(race_after) == 1, f"{name}: race day duplicated/dropped"
    assert _snap(race_after[0]) == before, (
        f"{name} rewrote the race session:\n was {before}\n now {_snap(race_after[0])}")


def test_e3_week_tier_down_race_immutable():
    goal, weeks, race = _race_fixture()
    plan = {"weeks": [{
        "week_num": w.week_num, "start": w.start.isoformat(),
        "end": w.end.isoformat(), "phase": w.phase,
        "tss_target": w.tss_target, "is_stepback": w.is_stepback,
        "sessions": [app_module._planned_session_to_json(s) for s in w.sessions],
    } for w in weeks]}
    before = copy.deepcopy(next(
        s for w in plan["weeks"] for s in w["sessions"]
        if s["day"] == goal.target_date.isoformat()))
    # Anchor ON the race day so the Mon-Sun walk window definitely covers it.
    tp.apply_week_tier_down(plan, goal.target_date.isoformat())
    after = next(s for w in plan["weeks"] for s in w["sessions"]
                 if s["day"] == goal.target_date.isoformat())
    assert after == before, "apply_week_tier_down touched the race day"


# ── E12 — availability hours=0 on the race date (dict path) ─────────────────

def test_e12_availability_zero_never_rests_race_day():
    goal, weeks, race = _race_fixture()
    before = _snap(race)
    tp.reforecast(goal, weeks, tsb_series={},
                  availability_overrides={goal.target_date.isoformat(): 0.0})
    after = next(s for w in weeks for s in w.sessions
                 if s.day == goal.target_date)
    assert after.session_type != "rest", (
        "hours=0 on the race date converted race day to rest (E12)")
    assert _snap(after) == before

    # Same through the FULL dict path (reforecast_dict + write-back).
    goal2, weeks2, _ = _race_fixture()
    pd = {
        "goal": {"type": "event", "event_date": goal2.target_date.isoformat(),
                 "event_name": "TestFondo", "event_km": 150.0,
                 "event_climb": 1500.0, "hours_per_week": 8.0,
                 "rest_days": [], "available_days": list(range(7))},
        "weeks": [{
            "week_num": w.week_num, "start": w.start.isoformat(),
            "end": w.end.isoformat(), "phase": w.phase,
            "tss_target": w.tss_target, "is_stepback": w.is_stepback,
            "sessions": [app_module._planned_session_to_json(s) for s in w.sessions],
        } for w in weeks2],
    }
    race_before = copy.deepcopy(next(
        s for w in pd["weeks"] for s in w["sessions"]
        if s["day"] == goal2.target_date.isoformat()))
    pd, _n, _i = tp.reforecast_dict(
        pd, today_iso=ANCHOR.isoformat(),
        availability_overrides={goal2.target_date.isoformat(): 0.0})
    race_after = next(s for w in pd["weeks"] for s in w["sessions"]
                      if s["day"] == goal2.target_date.isoformat())
    assert race_after == race_before, (
        "reforecast_dict availability rescale rewrote the persisted race day")


# ── L3-4 — _demote_hit_window demotes only pending, unpinned ────────────────

def test_l3_4_demote_hit_window_skips_frozen_state():
    goal, weeks, _ = _race_fixture()
    target = goal.target_date
    lib = tp.load_workout_library()
    week = weeks[1]
    # T-1 opener slot exists at target-1 in the fixture; repurpose the taper
    # week's slots into the audit's S11 victims.
    by_off = {(target - s.day).days: s for s in week.sessions}
    done = by_off[6]
    done.session_type, done.status = "vo2max", "done"
    dismissed = by_off[5]
    dismissed.session_type, dismissed.status = "threshold", "dismissed"
    dismissed.dismissed_at = "2026-04-25T08:00:00"
    dismissed.is_opener = False
    moved = by_off[3]
    moved.session_type, moved.user_moved = "sprint", True
    missed = by_off[4]
    missed.session_type, missed.status = "vo2max", "missed"
    pending = by_off[2]
    pending.session_type = "vo2max"  # the only legitimate demotion target
    pending.status = "pending"
    frozen_before = [_snap(s) for s in (done, dismissed, moved, missed)]
    pending_day = pending.day
    tp._demote_hit_window(weeks, target, 6, lib)
    assert [_snap(s) for s in (done, dismissed, moved, missed)] == frozen_before, (
        "eve-guard rewrote done/dismissed/user_moved/missed sessions (L3-4)")
    # The demotion REPLACES the session object — re-fetch it from the week.
    pending_after = next(s for s in week.sessions if s.day == pending_day)
    assert not tp._session_is_hit(pending_after), (
        "the pending unpinned hard in the window must still be demoted")


# ── L3-1 / FC5a — auto-move window exclusion + provenance ───────────────────

def _mini_plan(sessions, goal_extra=None):
    """One ISO-week plan dict around ANCHOR (a Monday)."""
    goal = {"type": "event",
            "event_date": (ANCHOR + timedelta(days=6)).isoformat(),
            "event_name": "TestFondo", "event_km": 150, "event_climb": 1500,
            "hours_per_week": 8.0, "rest_days": [],
            "available_days": [0, 1, 2, 3, 4, 5, 6]}
    goal.update(goal_extra or {})
    return {"goal": goal, "availability": {},
            "weeks": [{"week_num": 1, "start": ANCHOR.isoformat(),
                       "end": (ANCHOR + timedelta(days=6)).isoformat(),
                       "phase": "taper", "tss_target": 200,
                       "is_stepback": False, "sessions": sessions}]}


def _jsess(day, st, dur, **kw):
    d = {"day": day.isoformat(), "day_name": day.strftime("%a"),
         "session_type": st, "duration_min": dur, "tss_estimate": dur,
         "description": st, "zwo_file": "", "zwo_name": "", "status": "pending",
         "user_moved": False, "moved_from": "", "user_swapped": False,
         "completion_matches": None, "dismissed_at": "", "adapted": False,
         "is_race": False, "race": None, "is_opener": False}
    d.update(kw)
    return d


def _race_jsess(day, **kw):
    return _jsess(day, "recovery", 210, is_race=True,
                  race={"name": "TestFondo", "km": 150, "climb_m": 1500,
                        "type": "granfondo", "priority": "A"}, **kw)


def test_l3_1_missed_hard_never_auto_moved_into_event_window():
    # Race Sunday (T+6); missed vo2max Tuesday; the ONLY rest slot is Saturday
    # (= T-1). S14: the old code relocated the hard there and self-immunized.
    race_d = ANCHOR + timedelta(days=6)
    today = ANCHOR + timedelta(days=3)
    plan = _mini_plan([
        _jsess(ANCHOR, "z2", 60, status="done"),
        _jsess(ANCHOR + timedelta(days=1), "vo2max", 60, status="missed"),
        _jsess(ANCHOR + timedelta(days=2), "z2", 45, status="done"),
        _jsess(today, "z2", 45),
        _jsess(ANCHOR + timedelta(days=4), "z2", 45),
        _jsess(ANCHOR + timedelta(days=5), "rest", 0),
        _race_jsess(race_d),
    ])
    moves = app_module._auto_apply_missed_moves(plan, today)
    assert moves == [], f"hard session auto-moved into T-2..T+0: {moves}"
    eve = next(s for w in plan["weeks"] for s in w["sessions"]
               if s["day"] == (race_d - timedelta(days=1)).isoformat())
    assert eve["session_type"] == "rest", "race eve no longer a rest slot"
    assert not any(s.get("user_moved") for w in plan["weeks"]
                   for s in w["sessions"]), "auto path set user_moved (L3-1)"


def test_l3_1_auto_move_outside_window_sets_auto_moved_not_user_moved():
    race_d = ANCHOR + timedelta(days=6)
    today = ANCHOR + timedelta(days=2)
    plan = _mini_plan([
        _jsess(ANCHOR, "vo2max", 60, status="missed"),
        _jsess(ANCHOR + timedelta(days=1), "z2", 60, status="done"),
        _jsess(today, "z2", 45),
        _jsess(ANCHOR + timedelta(days=3), "rest", 0),  # T-3: legal target
        _jsess(ANCHOR + timedelta(days=4), "rest", 0),
        _jsess(ANCHOR + timedelta(days=5), "rest", 0),
        _race_jsess(race_d),
    ])
    moves = app_module._auto_apply_missed_moves(plan, today)
    assert moves, "expected the miss to relocate to the T-3 rest slot"
    assert moves[0]["to"] == (ANCHOR + timedelta(days=3)).isoformat(), (
        f"landed inside the protected window: {moves}")
    moved = next(s for w in plan["weeks"] for s in w["sessions"]
                 if s["day"] == moves[0]["to"])
    assert moved.get("auto_moved") is True
    assert moved.get("user_moved") is False, "auto move must NOT set user_moved"
    # The self-immunize chain is broken: the refit still sees it as touchable.
    dto = app_module._planned_session_from_json(moved)
    assert not tp._refit_session_frozen(dto, today), (
        "auto-moved session is frozen against the refit — L3-1 chain intact")


def test_l3_1_easy_missed_session_may_move_into_window():
    # The exclusion is for HARD destinations only — an easy z2 can still take
    # the eve rest slot (it's exactly what a taper wants there).
    race_d = ANCHOR + timedelta(days=6)
    today = ANCHOR + timedelta(days=3)
    plan = _mini_plan([
        _jsess(ANCHOR, "z2", 60, status="missed"),
        _jsess(ANCHOR + timedelta(days=1), "z2", 60, status="done"),
        _jsess(ANCHOR + timedelta(days=2), "z2", 45, status="done"),
        _jsess(today, "z2", 45),
        _jsess(ANCHOR + timedelta(days=4), "z2", 45),
        _jsess(ANCHOR + timedelta(days=5), "rest", 0),
        _race_jsess(race_d),
    ])
    suggestions = app_module._compute_missed_suggestions(plan, today)
    assert suggestions and suggestions[0]["suggested_date"] == (
        (ANCHOR + timedelta(days=5)).isoformat()), (
        "easy missed session should still be offered the eve rest slot")


# ── L3-2 — unridden race → terminal missed_race, never rescheduled ──────────

def test_l3_2_rematch_marks_unridden_race_missed_race():
    goal, weeks, race = _race_fixture()
    _FrozenDate._today = goal.target_date + timedelta(days=1)
    preview = tp.rematch_week(weeks[1], [], _FrozenDate.today())
    m = next(x for x in preview["matches"]
             if x["session_date"] == goal.target_date.isoformat())
    assert m["new_status"] == "missed_race", (
        f"unridden race classified {m['new_status']!r}, want terminal missed_race")
    # Terminal: once persisted, a re-run leaves it alone.
    race.status = "missed_race"
    preview2 = tp.rematch_week(weeks[1], [], _FrozenDate.today())
    assert not any(x["session_date"] == goal.target_date.isoformat()
                   for x in preview2["matches"])
    assert preview2["summary"].get("missed_race", 0) >= 1


def test_l3_2_race_never_auto_moved():
    race_past = ANCHOR + timedelta(days=1)
    today = ANCHOR + timedelta(days=2)
    plan = _mini_plan([
        _jsess(ANCHOR, "z2", 60, status="done"),
        _race_jsess(race_past, status="missed_race"),
        _jsess(today, "z2", 45),
        _jsess(ANCHOR + timedelta(days=3), "rest", 0),
        _jsess(ANCHOR + timedelta(days=4), "rest", 0),
        _jsess(ANCHOR + timedelta(days=5), "z2", 60),
        _jsess(ANCHOR + timedelta(days=6), "z2", 60),
    ], goal_extra={"event_date": race_past.isoformat()})
    # Legacy shape too: a plain "missed" status on the race entry (pre-Wave-2
    # plans) must be skipped by the is_race guard.
    legacy = copy.deepcopy(plan)
    legacy["weeks"][0]["sessions"][1]["status"] = "missed"
    for p in (plan, legacy):
        moves = app_module._auto_apply_missed_moves(p, today)
        assert moves == [], f"race entry auto-moved off its date: {moves}"
        slot = next(s for w in p["weeks"] for s in w["sessions"]
                    if s["day"] == race_past.isoformat())
        assert slot["is_race"], "race entry left its date"


# ── L3-6 — regen invariants ──────────────────────────────────────────────────

def test_l3_6_regen_mid_taper_keeps_state_and_respects_target():
    goal = _event_goal()
    _, weeks = tp.generate_plan(goal, seed_salt=0, **PLANNER_PIN_ARGS)
    target = goal.target_date
    moved_day = target - timedelta(days=4)
    dism_day = target - timedelta(days=2)
    for w in weeks:
        for s in w.sessions:
            # Force deterministic rider state onto the two days (whatever the
            # sampler put there): a user-dragged z2 and a dismissed z2. §6.12
            # keys on the FLAGS, not on the slot's previous content.
            if s.day == moved_day and not getattr(s, "is_race", False):
                s.session_type, s.duration_min, s.tss_estimate = "z2", 45, 34
                s.user_moved = True
            if s.day == dism_day and not getattr(s, "is_race", False):
                s.session_type, s.duration_min, s.tss_estimate = "z2", 45, 34
                s.status = "dismissed"
                s.dismissed_at = "2026-04-24T09:00:00"
    _FrozenDate._today = target - timedelta(days=6)
    new_phases, all_weeks, _info = tp.regenerate_from_today(
        goal, weeks, current_ctl=85.0, activities=[], seed_salt=5)
    # (a) no negative-span phases, (b) nothing beyond race + 2 weeks
    assert all(p.start <= p.end for p in new_phases), "negative-span phase"
    last_day = max(s.day for w in all_weeks for s in w.sessions)
    assert last_day <= target + timedelta(days=14), (
        f"regen scheduled {last_day}, past race+2w ({target + timedelta(days=14)})")
    # (c) §6.12 state survives even when the span is rebuilt as a ramp
    kept_m = [s for w in all_weeks for s in w.sessions
              if s.day == moved_day and getattr(s, "user_moved", False)]
    kept_d = [s for w in all_weeks for s in w.sessions
              if s.day == dism_day and getattr(s, "status", "") == "dismissed"]
    assert kept_m, "user_moved session lost in the regen (S06)"
    assert kept_d, "dismissed session resurrected by the regen (S06)"
    # (d) race day still the race — no z2 plowed through it
    on_target = [s for w in all_weeks for s in w.sessions if s.day == target]
    assert on_target and any(getattr(s, "is_race", False) for s in on_target)


def test_l3_6_regen_after_race_refuses():
    goal = _event_goal()
    _, weeks = tp.generate_plan(goal, seed_salt=0, **PLANNER_PIN_ARGS)
    _FrozenDate._today = goal.target_date + timedelta(days=1)
    with pytest.raises(ValueError, match="passed"):
        tp.regenerate_from_today(goal, weeks, current_ctl=80.0,
                                 activities=[], seed_salt=6)


# ── L3-13 — regen runs the volume + per-day clamps ──────────────────────────

def test_l3_13_regen_clamps_ramp_and_day_durations():
    goal = _event_goal(max_weekday_hours=1.0, max_weekend_hours=2.0)
    _, weeks = tp.generate_plan(goal, seed_salt=0, **PLANNER_PIN_ARGS)
    _FrozenDate._today = ANCHOR + timedelta(days=28)
    today = _FrozenDate.today()
    _, all_weeks, info = tp.regenerate_from_today(
        goal, weeks, current_ctl=50.0, activities=[], seed_salt=3)
    assert info["recovery_ramp_weeks"] > 0, "fixture: expected a gap → ramp"
    future = [w for w in all_weeks if w.start >= today]
    # per-day availability clamp: no weekday session over the 60-min cap
    over = [(s.day.isoformat(), s.session_type, s.duration_min)
            for w in future for s in w.sessions
            if s.session_type != "rest" and not getattr(s, "is_race", False)
            and s.day.weekday() < 5 and (s.duration_min or 0) > 60]
    assert over == [], f"regen weekday sessions exceed availability: {over[:6]}"
    # volume ceiling: ramp weeks trimmed to their own Gabbett-bounded targets
    ramps = [w for w in future if w.phase in ("recon", "recovery_ramp")]
    assert ramps, "fixture: expected ramp weeks"
    for w in ramps:
        planned = sum((s.tss_estimate or 0) for s in w.sessions
                      if s.session_type != "rest")
        assert planned <= w.tss_target * tp._VOLUME_CEILING_TOLERANCE + 1, (
            f"ramp week {w.week_num} planned {planned} vs target {w.tss_target} "
            "— the comeback ramp is still unclamped (S10)")


# ── L3-12 — refit re-owe ≤ 1.0× the missed dose near the race ───────────────

def test_l3_12_refit_reowe_capped_in_taper():
    goal = _event_goal()
    _, weeks = tp.generate_plan(goal, seed_salt=0, **PLANNER_PIN_ARGS)
    taper = next(w for w in weeks if w.phase == "taper")
    today = taper.start + timedelta(days=2)
    _FrozenDate._today = today
    missed = None
    for s in taper.sessions:
        # Overwrite the first pre-today slot (rest or not) — a missed HARD
        # record is what the refit keys on, regardless of the old slot type.
        if s.day < today and not getattr(s, "is_race", False):
            s.session_type = "vo2max"
            s.zwo_file = s.zwo_name = ""
            s.duration_min, s.tss_estimate = 45, 50
            s.status = "missed"
            missed = s
            break
    assert missed is not None, "fixture: no pre-today slot to mark missed"
    remaining = [s for s in taper.sessions
                 if s.day >= today and s.session_type != "rest"
                 and not tp._refit_session_frozen(s, today)]
    pre = sum((s.tss_estimate or 0) for s in remaining)
    pre_days = {s.day for s in remaining}
    _, info = tp.refit_remaining_week(goal, weeks, today, seed_salt=55)
    post = sum((s.tss_estimate or 0) for s in taper.sessions
               if s.day in pre_days and s.session_type != "rest")
    assert post - pre <= (missed.tss_estimate or 0) + 1, (
        f"refit re-owed {post - pre:.0f} TSS for a {missed.tss_estimate:.0f}-TSS "
        f"miss inside the taper (S02b was 2.7×); info={info}")


# ── app-layer endpoints (L3-7 / L3-10 / L3-11 / move) ───────────────────────

class _Req:
    def __init__(self, body):
        self._b = body
        self.url = type("U", (), {"path": "/test"})()

    async def json(self):
        return self._b


@pytest.fixture()
def _app_plan_env(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "_plan_dir", lambda: tmp_path)
    monkeypatch.setattr(app_module.db, "query_activities",
                        lambda **k: [], raising=False)
    monkeypatch.setattr(app_module, "cached", lambda *a, **k: {})

    def write(plan):
        (tmp_path / "current_plan.json").write_text(json.dumps(plan))

    def read():
        return json.loads((tmp_path / "current_plan.json").read_text())

    return write, read


def test_l3_7_auto_adjust_rest_never_wipes_race_day(_app_plan_env):
    write, read = _app_plan_env
    race_tmrw = date.today() + timedelta(days=1)
    monday = date.today() - timedelta(days=date.today().weekday())
    # Two stored weeks so "tomorrow" is covered even when today is Sunday.
    weeks = []
    for wn in (0, 1):
        ws = monday + timedelta(days=7 * wn)
        weeks.append({"week_num": wn + 1, "start": ws.isoformat(),
                      "end": (ws + timedelta(days=6)).isoformat(),
                      "phase": "taper", "tss_target": 200, "is_stepback": False,
                      "sessions": [_jsess(ws + timedelta(days=i), "z2", 45)
                                   for i in range(7)]})
    plan = {"goal": {"type": "event", "event_date": race_tmrw.isoformat()},
            "availability": {}, "weeks": weeks}
    for w in plan["weeks"]:
        for i, s in enumerate(w["sessions"]):
            if s["day"] == race_tmrw.isoformat():
                w["sessions"][i] = _race_jsess(race_tmrw)
    race_before = copy.deepcopy(next(
        s for w in plan["weeks"] for s in w["sessions"]
        if s["day"] == race_tmrw.isoformat()))
    write(plan)
    resp = asyncio.run(app_module.api_plan_auto_adjust(
        _Req({"severity": "rest", "scope": "day"})))
    assert isinstance(resp, dict) and resp["ok"]
    assert resp["applied"] is False and resp["actions"] == []
    assert "race day is fixed" in resp["note"]
    after = next(s for w in read()["weeks"] for s in w["sessions"]
                 if s["day"] == race_tmrw.isoformat())
    assert after == race_before, "severity=rest wiped the race day (L3-7)"


def test_l3_10_swap_type_race_day_refused():
    race_d = ANCHOR + timedelta(days=6)
    plan = _mini_plan([_jsess(ANCHOR, "z2", 60), _race_jsess(race_d)])
    before = copy.deepcopy(plan["weeks"][0]["sessions"][1])
    with pytest.raises(ValueError, match="edit the race instead"):
        app_module._swap_session_type_apply(plan, race_d.isoformat(), "vo2max", 60)
    assert plan["weeks"][0]["sessions"][1] == before


def test_l3_10_redraw_race_day_refused():
    race_d = ANCHOR + timedelta(days=6)
    plan = _mini_plan([_jsess(ANCHOR, "z2", 60), _race_jsess(race_d)])
    before = copy.deepcopy(plan["weeks"][0]["sessions"][1])
    with pytest.raises(ValueError, match="edit the race instead"):
        app_module._pick_redraw_candidate(plan, race_d.isoformat())
    with pytest.raises(ValueError, match="edit the race instead"):
        app_module._accept_redraw_apply(plan, race_d.isoformat(),
                                        {"zwo_file": "x.zwo", "zwo_name": "X"})
    assert plan["weeks"][0]["sessions"][1] == before


def test_l3_11_dismiss_hits_all_copies_of_a_date(_app_plan_env):
    write, read = _app_plan_env
    dup_d = ANCHOR + timedelta(days=4)
    plan = _mini_plan([_jsess(ANCHOR, "z2", 60), _jsess(dup_d, "z2", 90)])
    plan["weeks"].append({
        "week_num": 2, "start": dup_d.isoformat(),
        "end": (dup_d + timedelta(days=6)).isoformat(), "phase": "taper",
        "tss_target": 200, "is_stepback": False,
        "sessions": [_jsess(dup_d, "threshold", 45),
                     _jsess(dup_d + timedelta(days=1), "z2", 60)]})
    write(plan)
    resp = asyncio.run(app_module.api_plan_dismiss_session(
        _Req({"date": dup_d.isoformat()})))
    assert isinstance(resp, dict) and resp["ok"]
    copies = [s for w in read()["weeks"] for s in w["sessions"]
              if s["day"] == dup_d.isoformat()]
    assert len(copies) == 2
    assert all(s["status"] == "dismissed" and s["dismissed_at"]
               for s in copies), (
        f"dismiss only hit the first copy (L3-11): {copies}")


def test_fc3_dismiss_race_day_400(_app_plan_env):
    write, read = _app_plan_env
    race_d = ANCHOR + timedelta(days=6)
    plan = _mini_plan([_jsess(ANCHOR, "z2", 60), _race_jsess(race_d)])
    write(plan)
    resp = asyncio.run(app_module.api_plan_dismiss_session(
        _Req({"date": race_d.isoformat()})))
    assert getattr(resp, "status_code", 200) == 400
    slot = next(s for w in read()["weeks"] for s in w["sessions"]
                if s["day"] == race_d.isoformat())
    assert slot["status"] == "pending" and slot["is_race"]


def test_fc3_move_race_day_400(_app_plan_env):
    write, read = _app_plan_env
    race_d = ANCHOR + timedelta(days=6)
    dst = ANCHOR + timedelta(days=3)
    plan = _mini_plan([_jsess(ANCHOR, "z2", 60), _jsess(dst, "rest", 0),
                       _race_jsess(race_d)])
    write(plan)
    # Drag the race off its date → 400; drag something ONTO the race → 400.
    for body in ({"date": race_d.isoformat(), "new_date": dst.isoformat()},
                 {"date": ANCHOR.isoformat(), "new_date": race_d.isoformat()}):
        resp = asyncio.run(app_module.api_plan_move_session(_Req(body)))
        assert getattr(resp, "status_code", 200) == 400, body
        assert b"edit the race instead" in resp.body
    slot = next(s for w in read()["weeks"] for s in w["sessions"]
                if s["day"] == race_d.isoformat())
    assert slot["is_race"], "race entry moved despite the guard"
    # The shared mutator refuses too (auto-path defense).
    assert app_module._apply_move_session(
        plan, race_d.isoformat(), dst.isoformat()) is None


# ── D4 / F4c — race-week micro-plan ─────────────────────────────────────────

def _micro_hard(weeks):
    return [s for w in weeks for s in w.sessions
            if tp._session_is_hit(s) and not getattr(s, "is_opener", False)
            and not getattr(s, "is_race", False)]


def test_d4_race_tomorrow_micro_plan():
    target = ANCHOR + timedelta(days=1)
    phases, weeks = tp.generate_plan(_event_goal(target),
                                     seed_salt=0, **PLANNER_PIN_ARGS)
    assert len(phases) == 1, "micro-plan is a single race-week phase"
    assert phases[0].start == ANCHOR and phases[0].end == target
    assert not [s for w in weeks for s in w.sessions if s.day > target], (
        "training scheduled after race day (D4)")
    assert any(getattr(s, "is_race", False)
               for w in weeks for s in w.sessions if s.day == target)
    assert len(_micro_hard(weeks)) <= 1, "micro-plan exceeds one hard touch"


def test_d4_ten_day_runway_micro_plan_shape():
    target = ANCHOR + timedelta(days=10)
    phases, weeks = tp.generate_plan(_event_goal(target),
                                     seed_salt=0, **PLANNER_PIN_ARGS)
    assert len(phases) == 1 and phases[0].name == "taper"
    assert "micro-plan" in phases[0].focus.lower()
    assert phases[0].start == ANCHOR and phases[0].end == target
    # clip invariants hold on the micro emission
    days = [s.day for w in weeks for s in w.sessions]
    assert len(days) == len(set(days)) and max(days) <= target
    assert len(_micro_hard(weeks)) <= 1, (
        f"{len(_micro_hard(weeks))} hard touches in a 10-day micro-plan")
    # Wave-1 opener shape at T-1
    eve = next(s for w in weeks for s in w.sessions
               if s.day == target - timedelta(days=1))
    assert getattr(eve, "is_opener", False) or eve.session_type == "rest"
    # rest/openers/race composition over the final 4 days: nothing but rest,
    # the opener, a short easy spin (T-2 rule: ≤45min z1) and the race itself.
    for s in (s for w in weeks for s in w.sessions
              if 0 <= (target - s.day).days <= 3):
        ok = (getattr(s, "is_race", False) or getattr(s, "is_opener", False)
              or s.session_type == "rest"
              or (s.session_type in ("z2", "recovery")
                  and (s.duration_min or 0) <= 45))
        assert ok, (f"final-days slot is not rest/opener/easy/race: "
                    f"{s.day} {s.session_type} {s.duration_min}m")


def test_d4_generate_endpoint_carries_warning(_app_plan_env, tmp_path,
                                              monkeypatch):
    write, read = _app_plan_env
    monkeypatch.setattr(app_module, "cached",
                        lambda *a, **k: {"ctl": 50.0})
    import ride_storage
    monkeypatch.setattr(ride_storage, "recent_mean_weekly_tss",
                        lambda: 650.0, raising=False)
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    target = ANCHOR + timedelta(days=1)
    resp = asyncio.run(app_module.api_plan_generate(_Req({
        "goal": "event", "event_date": target.isoformat(),
        "event_name": "TestFondo", "event_km": 150, "event_climb": 1500,
        # keep the endpoint off the machine-local ride archive
        "longest_ride_h_90d": 3.0,
    })))
    assert isinstance(resp, dict), getattr(resp, "body", resp)
    assert resp["ok"]
    assert "race-week plan" in resp.get("warning", ""), (
        "F4c warning missing from the response payload")
    assert resp["plan_json"].get("warning") == resp["warning"], (
        "warning must persist in the plan payload too")
    hard = [s for w in resp["plan_json"]["weeks"] for s in w["sessions"]
            if s["session_type"] in ("vo2max", "threshold", "overunder",
                                     "sweetspot", "sprint")
            and not s.get("is_opener") and not s.get("is_race")]
    assert len(hard) <= 1
    assert not [s for w in resp["plan_json"]["weeks"] for s in w["sessions"]
                if s["day"] > target.isoformat()]


def test_f4c_normal_runway_has_no_warning():
    # 16w runway: multi-phase plan, no micro warning path.
    phases, _ = tp.generate_plan(_event_goal(TARGET_16W),
                                 seed_salt=0, **PLANNER_PIN_ARGS)
    assert len(phases) > 1, "16w plan must not collapse to a micro-plan"
