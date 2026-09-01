"""Wave 1 regression tests for the P4.1 event-planner audit (v2.5.0).

Converted from the audit repros (scratchpad p41_l2_repro_1..4 → D1-D4,
p41_l1_repro_5..7 → D5-D7, p41_l3_repro_1 S08 → L3-3, plus SM2/SM5 evidence)
using the W8 pinned-env pattern (tests/conftest.py): current_ctl=50.0,
recent_weekly_tss=650.0, frozen today=2026-01-05 (Monday), seed_salt=0.

Wave 1 items covered here:
  F4b  (D1)   — generate_plan raises ValueError on target_date <= today
  F4d  (SM4)  — second priority-A event refused ("one A event per plan")
  FC1  (D2)   — clip: zero duplicate days, true row count, disjoint spans
  FC1  (D3)   — no training after race day
  FC1  (D4)   — race-tomorrow: clip guarantees (full F4c micro-plan deferred)
  FC2a (D5)   — taper volume enforced, descending into the race
  F2b  (D6)   — race-week composition: openers, T-2 easy, duration caps, rest
  FC2a (D7)   — blueprint modes get the same taper via the post-passes
  FC4a (L3-3) — reforecast_dict rebuilds a FULL goal; race guards alive
  E9          — _mark_race_days self-clamping + idempotent
  E7          — plan-dict round-trip of is_race/race/user_moved/dismissed_at/
                is_opener; race day never rewritten by the write-back
  SM2         — Phase.weeks labels derived from actual spans
  SM5         — non-event goals with a target_date cover through target-1
  F2d  (SM1)  — >20-week base splits into ≤4-week blocks
  SM3         — B race: B-1 opener + B+1..2 easy
"""
from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path

import pytest

import training_planner as tp
from conftest import PLANNER_PIN_ANCHOR as ANCHOR, PLANNER_PIN_ARGS

_LIB_INDEX = Path(__file__).resolve().parent.parent / "src" / "workouts" / ".library_index.json"

# Monday race, 16 weeks out (D3's worst observed form was a Monday race).
TARGET_16W = ANCHOR + timedelta(days=112)

_HIT_TYPES = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}


@pytest.fixture(scope="module", autouse=True)
def _pinned_env(planner_pinned_env):
    """W8 pin (frozen date + stubbed ICU fetch) + library-index restore so the
    module leaves the tracked cache byte-identical."""
    backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None
    yield
    if backup is not None and _LIB_INDEX.read_bytes() != backup:
        _LIB_INDEX.write_bytes(backup)


def _event_goal(target, **kw):
    base = dict(goal_type="event", target_date=target, event_name="TestFondo",
                event_km=150.0, event_climb_m=1500.0)
    base.update(kw)
    return tp.Goal(**base)


def _wtss(w, include_race=True):
    return round(sum((s.tss_estimate or 0) for s in w.sessions
                     if s.session_type != "rest"
                     and (include_race or not getattr(s, "is_race", False))))


def _full_builds(weeks):
    """Full-span (7-day) non-stepback non-taper rows — the honest pre-taper
    reference set (E11: stepbacks exempt; FC1 clip rows excluded)."""
    return [w for w in weeks if w.phase != "taper" and not w.is_stepback
            and (w.end - w.start).days + 1 >= 7]


@pytest.fixture(scope="module")
def plan_16w():
    goal = _event_goal(TARGET_16W)
    phases, weeks = tp.generate_plan(goal, seed_salt=0, **PLANNER_PIN_ARGS)
    return goal, phases, weeks


# ── F4b (D1) — past/today target refused ────────────────────────────────────

def test_d1_past_target_date_raises():
    with pytest.raises(ValueError, match="past"):
        tp.generate_plan(_event_goal(ANCHOR - timedelta(days=30)),
                         seed_salt=0, **PLANNER_PIN_ARGS)


def test_d1_today_target_date_raises():
    with pytest.raises(ValueError, match="future"):
        tp.generate_plan(_event_goal(ANCHOR), seed_salt=0, **PLANNER_PIN_ARGS)


def test_d1_tomorrow_target_ok_boundary():
    phases, weeks = tp.generate_plan(_event_goal(ANCHOR + timedelta(days=1)),
                                     seed_salt=0, **PLANNER_PIN_ARGS)
    assert weeks, "tomorrow is a legal (1-day-runway) target"
    assert all(s.day <= ANCHOR + timedelta(days=1)
               for w in weeks for s in w.sessions)


# ── F4d (SM4) — second priority-A event refused ─────────────────────────────

def test_f4d_second_priority_a_event_raises():
    goal = _event_goal(TARGET_16W, events=[
        tp.TargetEvent(date=TARGET_16W - timedelta(days=35), priority="A",
                       name="Rogue second A", event_km=120),
    ])
    with pytest.raises(ValueError, match="one A event per plan"):
        tp.generate_plan(goal, seed_salt=0, **PLANNER_PIN_ARGS)


# ── FC1 (D2) — clip invariants on the 16w seam ──────────────────────────────

def test_d2_no_duplicate_days_disjoint_rows_true_count(plan_16w):
    _, phases, weeks = plan_16w
    seen_days = {}
    for w in weeks:
        for s in w.sessions:
            assert w.start <= s.day <= w.end, "session outside its row span"
            assert s.day not in seen_days, (
                f"{s.day} scheduled twice (rows {seen_days[s.day]} and {w.week_num})")
            seen_days[s.day] = w.week_num
    # Disjoint + contiguous row spans, union = [plan start, target].
    rows = sorted(weeks, key=lambda w: w.start)
    assert rows[0].start == ANCHOR
    assert rows[-1].end == TARGET_16W
    for a, b in zip(rows, rows[1:]):
        assert (b.start - a.end).days == 1, f"gap/overlap between rows {a.week_num}/{b.week_num}"
    # Every row belongs to exactly ONE phase.
    for w in rows:
        owners = [p for p in phases if p.start <= w.start and w.end <= p.end
                  and p.name == w.phase]
        assert owners, f"row {w.week_num} ({w.start}..{w.end}) crosses phase boundaries"
    # Row count equals the span-derived week sum (the old sum(p.weeks) lied:
    # 16 labeled vs 17 emitted).
    assert len(weeks) == sum(tp._span_weeks(p) for p in phases)


# ── FC1 (D3) — nothing after race day ───────────────────────────────────────

def test_d3_no_training_after_race_day(plan_16w):
    _, _, weeks = plan_16w
    after = [s for w in weeks for s in w.sessions if s.day > TARGET_16W]
    assert after == [], f"sessions scheduled after the race: {[(str(s.day), s.session_type) for s in after]}"
    on_target = [s for w in weeks for s in w.sessions if s.day == TARGET_16W]
    assert on_target and any(getattr(s, "is_race", False) for s in on_target)


# ── FC1 (D4) — race tomorrow: clip guarantees hold ──────────────────────────

def test_d4_race_tomorrow_no_training_after_race():
    target = ANCHOR + timedelta(days=1)
    phases, weeks = tp.generate_plan(_event_goal(target),
                                     seed_salt=0, **PLANNER_PIN_ARGS)
    assert len(weeks) == 1
    assert weeks[0].end == target, "row clipped at the race, not target+5"
    assert not [s for w in weeks for s in w.sessions if s.day > target]
    assert any(getattr(s, "is_race", False)
               for w in weeks for s in w.sessions if s.day == target)
    # SM2 on the same fixture: 2-day span is labeled 1 week (was 2).
    assert phases[-1].name == "taper" and phases[-1].weeks == 1


@pytest.mark.xfail(
    reason="F4c deferred to Wave 2 (resequenced after FC1+FC2): a <14d runway "
           "should emit a race-week-only MICRO-PLAN (rest/openers/race, no "
           "full taper phase) plus a response warning field.",
    strict=False)
def test_d4_f4c_micro_plan_deferred_marker():
    phases, weeks = tp.generate_plan(_event_goal(ANCHOR + timedelta(days=10)),
                                     seed_salt=0, **PLANNER_PIN_ARGS)
    assert len(phases) == 1 and phases[0].name == "race_week"
    hard = [s for w in weeks for s in w.sessions
            if tp._session_is_hit(s) and not getattr(s, "is_opener", False)
            and not getattr(s, "is_race", False)]
    assert len(hard) <= 1


# ── FC2a (D5) — taper volume enforced, descending ───────────────────────────

def test_d5_taper_volume_descends_into_race(plan_16w):
    _, _, weeks = plan_16w
    tapers = [w for w in weeks if w.phase == "taper"]
    assert len(tapers) == 2
    builds = _full_builds(weeks)
    peak_ref = max(_wtss(w) for w in builds[-3:])
    t1 = _wtss(tapers[0])
    race_training = _wtss(tapers[1], include_race=False)
    assert t1 <= 0.65 * peak_ref, (
        f"taper wk1 {t1} > 65% of actual pre-taper max {peak_ref}")
    assert race_training < t1, (
        f"race week training {race_training} not below taper wk1 {t1}")
    assert t1 < peak_ref, "volume must DESCEND into the race, not ascend"


# ── F2b (D6) — race-week composition ────────────────────────────────────────

def test_d6_race_week_composition(plan_16w):
    _, _, weeks = plan_16w
    final = {(TARGET_16W - s.day).days: s
             for w in weeks for s in w.sessions
             if 0 <= (TARGET_16W - s.day).days <= 6}
    eve = final[1]
    assert getattr(eve, "is_opener", False), "T-1 must be the openers ride"
    assert eve.session_type == "rest" or eve.duration_min <= 50
    assert "opener" in (eve.description or "").lower()
    t2 = final[2]
    assert t2.session_type == "rest" or (
        t2.session_type in ("recovery", "z2") and t2.duration_min <= 45), (
        f"T-2 must be rest or a ≤45min z1 spin, got {t2.session_type} {t2.duration_min}m")
    for back in (1, 2, 3):
        s = final[back]
        assert (s.duration_min or 0) < 90, (
            f"≥90min ride at T-{back}: {s.session_type} {s.duration_min}m")
    race_wk = next(w for w in weeks if w.start <= TARGET_16W <= w.end)
    rest_days = sum(1 for s in race_wk.sessions
                    if s.session_type == "rest" and s.day != TARGET_16W)
    assert rest_days >= 3, f"race week has {rest_days} rest days, need ≥3"


# ── FC2a (D7) — blueprint modes fixed by the same post-passes ───────────────

@pytest.mark.parametrize("mode,template_id", [
    ("fixed_core", ""),
    ("template", "polarized_base"),
    ("template", "ftp_builder"),
])
def test_d7_blueprint_modes_get_taper_and_openers(mode, template_id):
    goal = _event_goal(TARGET_16W, plan_mode=mode, template_id=template_id)
    _, weeks = tp.generate_plan(goal, seed_salt=0, **PLANNER_PIN_ARGS)
    tapers = [w for w in weeks if w.phase == "taper"]
    builds = _full_builds(weeks)
    peak_ref = max(_wtss(w) for w in builds[-3:])
    t1 = _wtss(tapers[0])
    race_training = _wtss(tapers[-1], include_race=False)
    assert t1 <= 0.65 * peak_ref
    assert race_training < t1
    eve = next(s for w in weeks for s in w.sessions
               if (TARGET_16W - s.day).days == 1)
    assert getattr(eve, "is_opener", False) and eve.duration_min <= 50
    assert not [s for w in weeks for s in w.sessions if s.day > TARGET_16W]


# ── FC4a (L3-3) — reforecast_dict event guards alive ────────────────────────

def _race_week_plan_dict(target):
    """The audit's S08 fixture: one taper week ending on the race, a vo2max on
    the eve, the race day persisted with is_race + meta."""
    eve = target - timedelta(days=1)
    sessions = []
    for k in range(7):
        d = target - timedelta(days=6 - k)
        st, dur = "z2", 60
        if d == eve:
            st, dur = "vo2max", 60
        if d == target:
            st, dur = "recovery", 240
        sessions.append({
            "day": d.isoformat(), "day_name": d.strftime("%a"),
            "session_type": st, "duration_min": dur, "tss_estimate": 60,
            "description": "x", "zwo_file": "", "zwo_name": "",
            "status": "pending",
            "is_race": d == target,
            "race": ({"name": "TestFondo", "km": 150.0, "climb_m": 1500.0,
                      "type": "granfondo", "priority": "A"}
                     if d == target else None),
        })
    return {
        "goal": {"type": "event", "event_date": target.isoformat(),
                 "event_name": "TestFondo", "event_km": 150,
                 "event_climb": 1500, "hours_per_week": 8, "rest_days": [0]},
        "weeks": [{"week_num": 15, "start": sessions[0]["day"],
                   "end": sessions[-1]["day"], "phase": "taper",
                   "tss_target": 275, "is_stepback": False,
                   "sessions": sessions}],
    }


def test_l3_3_reforecast_dict_eve_guard_alive():
    target = ANCHOR + timedelta(days=112)
    pd = _race_week_plan_dict(target)
    pd, _, _ = tp.reforecast_dict(pd, today_iso=ANCHOR.isoformat())
    eve = next(s for w in pd["weeks"] for s in w["sessions"]
               if s["day"] == (target - timedelta(days=1)).isoformat())
    assert eve["session_type"] not in _HIT_TYPES, (
        "race-eve vo2max survived reforecast_dict — the event guard is dead "
        "again (goal rebuild lost target_date)")


def test_l3_3_reforecast_dict_never_rewrites_race_day():
    target = ANCHOR + timedelta(days=112)
    pd = _race_week_plan_dict(target)
    before = copy.deepcopy(next(
        s for w in pd["weeks"] for s in w["sessions"]
        if s["day"] == target.isoformat()))
    pd, _, _ = tp.reforecast_dict(pd, today_iso=ANCHOR.isoformat())
    after = next(s for w in pd["weeks"] for s in w["sessions"]
                 if s["day"] == target.isoformat())
    assert after == before, "persisted race day was rewritten by reforecast"


def test_fc4a_target_events_parser():
    evs = tp._target_events_from_dicts([
        {"date": "2026-08-01", "priority": "B", "name": "B1", "event_km": 90,
         "event_climb_m": 800},
        {"date": "2026-09-01", "priority": "C", "event_climb": 300},
        {"date": "not-a-date"},
        {"name": "missing date"},
        "garbage",
    ])
    assert [(e.date.isoformat(), e.priority, e.event_climb_m) for e in evs] == [
        ("2026-08-01", "B", 800), ("2026-09-01", "C", 300)]


# ── E9 — _mark_race_days self-clamping + idempotent ─────────────────────────

def test_e9_mark_race_days_self_clamps_and_is_idempotent():
    target = ANCHOR + timedelta(days=112)
    goal = _event_goal(target)
    pw = tp._plan_dict_to_planned_weeks(_race_week_plan_dict(target))
    tp._mark_race_days(pw, goal)
    race = next(s for w in pw for s in w.sessions if s.day == target)
    cap_min = int(goal.max_hours_for_day(target.weekday()) * 60)
    assert race.is_race and race.race
    assert race.duration_min <= cap_min, (
        f"race day {race.duration_min}min exceeds the {cap_min}min day cap — "
        "self-clamp missing (churn source on reforecast/regen paths)")
    assert race.race.get("est_duration_min", 0) > cap_min, (
        "meta must keep the TRUE race estimate for the UI")
    snap1 = [(s.day, s.session_type, s.duration_min, s.tss_estimate,
              s.is_race, s.race) for w in pw for s in w.sessions]
    tp._mark_race_days(pw, goal)
    snap2 = [(s.day, s.session_type, s.duration_min, s.tss_estimate,
              s.is_race, s.race) for w in pw for s in w.sessions]
    assert snap1 == snap2, "_mark_race_days is not idempotent (race-card churn)"


# ── E7 — plan-dict round-trip of the protection fields ──────────────────────

def test_e7_round_trip_carries_protection_fields():
    target = ANCHOR + timedelta(days=112)
    pd = _race_week_plan_dict(target)
    s0 = pd["weeks"][0]["sessions"][0]
    s0["user_moved"] = True
    s0["dismissed_at"] = "2026-01-02T10:00:00"
    s1 = pd["weeks"][0]["sessions"][1]
    s1["is_opener"] = True
    pw = tp._plan_dict_to_planned_weeks(pd)
    r0, r1 = pw[0].sessions[0], pw[0].sessions[1]
    assert r0.user_moved is True and r0.dismissed_at == "2026-01-02T10:00:00"
    assert r1.is_opener is True
    race = next(s for s in pw[0].sessions if s.day == target)
    assert race.is_race is True and (race.race or {}).get("name") == "TestFondo"


def test_e7_apply_reforecast_writes_new_fields_and_skips_race():
    target = ANCHOR + timedelta(days=112)
    pd = _race_week_plan_dict(target)
    pw = tp._plan_dict_to_planned_weeks(pd)
    # Simulate a reforecast that installed an opener on the eve and tried to
    # rewrite the race day.
    eve = target - timedelta(days=1)
    for s in pw[0].sessions:
        if s.day == eve:
            s.session_type = "z2"
            s.duration_min = 45
            s.is_opener = True
        if s.day == target:
            s.session_type = "z2"       # hostile rewrite attempt
            s.duration_min = 30
    touched = {eve.isoformat(), target.isoformat()}
    tp._apply_reforecast_to_dict(pd, pw, touched)
    eve_json = next(s for w in pd["weeks"] for s in w["sessions"]
                    if s["day"] == eve.isoformat())
    race_json = next(s for w in pd["weeks"] for s in w["sessions"]
                     if s["day"] == target.isoformat())
    assert eve_json["is_opener"] is True and eve_json["session_type"] == "z2"
    assert race_json["session_type"] == "recovery" and race_json["duration_min"] == 240, (
        "a persisted race day must NEVER be rewritten by the reforecast write-back")


# ── SM2 — phase week labels from actual spans ───────────────────────────────

def test_sm2_phase_week_labels_match_spans(plan_16w):
    _, phases, weeks = plan_16w
    taper = next(p for p in phases if p.name == "taper")
    span = (taper.end - taper.start).days + 1
    assert taper.weeks == -(-span // 7), (
        f"taper labeled {taper.weeks}w but spans {span}d")
    assert taper.end == TARGET_16W, "taper ends ON the target date"
    assert sum(tp._span_weeks(p) for p in phases) == len(weeks)


# ── SM5 — non-event goals cover through the target eve ──────────────────────

def test_sm5_non_event_goal_covers_target_eve():
    target = ANCHOR + timedelta(days=110)  # 110//7=15 → old undershoot 6 days
    goal = tp.Goal(goal_type="ftp", target_date=target, hours_per_week=10.0)
    _, weeks = tp.generate_plan(goal, seed_salt=0, **PLANNER_PIN_ARGS)
    last_day = max(s.day for w in weeks for s in w.sessions)
    assert (target - last_day).days <= 1, (
        f"plan stops {(target - last_day).days}d short of the target date")
    assert last_day < target, "non-event goals place nothing ON the date itself"


def test_sm5_exact_multiple_runway_untouched():
    # The W8 pinned-suite shape: general goal, target exactly 24 weeks out.
    # Already ends at target-1 → SM5 must not restructure it (E4 parity).
    goal = tp.Goal(goal_type="general",
                   target_date=ANCHOR + timedelta(weeks=24),
                   hours_per_week=10.0)
    phases = tp.generate_phases(goal, 50.0, None, recent_weekly_tss=650.0)
    assert phases[-1].end == goal.target_date - timedelta(days=1)
    assert sum(tp._span_weeks(p) for p in phases) == sum(p.weeks for p in phases) == 24


# ── F2d (SM1) — oversized base splits into blocks ───────────────────────────

def test_f2d_long_base_splits_into_4w_blocks():
    goal = _event_goal(ANCHOR + timedelta(weeks=36))
    phases = tp.generate_phases(goal, 50.0, None, recent_weekly_tss=650.0)
    base = [p for p in phases if p.name == "base"]
    assert len(base) > 1, "23-week base must split into blocks"
    assert all(p.weeks <= 4 for p in base)
    assert all("block" in p.focus for p in base)
    for a, b in zip(phases, phases[1:]):
        assert (b.start - a.end).days == 1, "blocks stay contiguous"


def test_f2d_short_base_stays_monolithic():
    goal = _event_goal(ANCHOR + timedelta(weeks=16))
    phases = tp.generate_phases(goal, 50.0, None, recent_weekly_tss=650.0)
    assert len([p for p in phases if p.name == "base"]) == 1


# ── SM3 — B race gets the opener + post-race easy days ──────────────────────

def test_sm3_b_race_opener_and_recovery():
    b_date = TARGET_16W - timedelta(days=42)
    goal = _event_goal(TARGET_16W, events=[
        tp.TargetEvent(date=b_date, priority="B", name="B race", event_km=90)])
    _, weeks = tp.generate_plan(goal, seed_salt=0, **PLANNER_PIN_ARGS)
    bm1 = next(s for w in weeks for s in w.sessions
               if s.day == b_date - timedelta(days=1))
    assert getattr(bm1, "is_opener", False), "B-1 must be an opener"
    b_day = next(s for w in weeks for s in w.sessions if s.day == b_date)
    assert b_day.is_race
    post = [s for w in weeks for s in w.sessions
            if b_date < s.day <= b_date + timedelta(days=2)]
    assert post and all(not tp._session_is_hit(s) for s in post), (
        "B+1..B+2 must be easy (no HIT)")
