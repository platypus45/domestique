"""PART B (IP_PLAN_CONTINUITY, v3.1.0) — mid-plan entry: engine-level tests.

GB1  start_date=None ⇒ byte-identical planner behavior (same-process paired
     A/B serialized weeks; anchor==today unit test; zero extra global RNG
     draws). NO cross-run golden files.
GB2  backdated event goal: full-runway phase split, correct position, no
     scheduled sessions in the past, taper/race guards hold (event-week
     matrix pattern with backdating).
+    B-LOCKED-2 ramp clamp (remaining-weeks credit), B-LOCKED-5 override
     precedence, input-gate errors, elapsed-strip semantics, FTP-test
     retarget to the first schedulable week.

Pinned env (W8 pattern): frozen today = 2026-01-05 (Monday), current_ctl=50,
recent_weekly_tss=650, seed_salt=0. Hermetic — no HOME writes, no network.
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta

import pytest

import training_planner as tp
from conftest import PLANNER_PIN_ANCHOR as ANCHOR, PLANNER_PIN_ARGS


@pytest.fixture(scope="module", autouse=True)
def _env(planner_pinned_env):
    yield


# ── helpers ──────────────────────────────────────────────────────────────────

def _serialize_weeks(weeks) -> bytes:
    """Canonical byte serialization of PlannedWeek rows (mirrors the app
    write-site core fields)."""
    out = []
    for w in weeks:
        out.append({
            "week_num": w.week_num,
            "start": w.start.isoformat(), "end": w.end.isoformat(),
            "phase": w.phase, "tss_target": w.tss_target,
            "is_stepback": w.is_stepback,
            "sessions": [
                {
                    "day": s.day.isoformat(), "day_name": s.day_name,
                    "session_type": s.session_type,
                    "duration_min": s.duration_min,
                    "tss_estimate": s.tss_estimate,
                    "description": s.description,
                    "zwo_file": s.zwo_file, "zwo_name": s.zwo_name,
                    "is_race": bool(getattr(s, "is_race", False)),
                    "is_opener": bool(getattr(s, "is_opener", False)),
                }
                for s in w.sessions
            ],
        })
    return json.dumps(out, sort_keys=True).encode()


def _gen(goal, seed_salt=0):
    return tp.generate_plan(goal, seed_salt=seed_salt, **PLANNER_PIN_ARGS)


# ── GB1 — None ⇒ byte-identical + zero extra RNG draws ──────────────────────

def test_gb1_paired_none_field_byte_equal_weeks():
    goal_a = tp.Goal(goal_type="general", plan_weeks=8, hours_per_week=8.0)
    goal_b = tp.Goal(goal_type="general", plan_weeks=8, hours_per_week=8.0,
                     start_date=None, entry_mode=None)

    random.seed(987654321)
    phases_a, weeks_a = _gen(goal_a)
    rng_state_a = random.getstate()

    random.seed(987654321)
    phases_b, weeks_b = _gen(goal_b)
    rng_state_b = random.getstate()

    assert _serialize_weeks(weeks_a) == _serialize_weeks(weeks_b)
    assert [(p.name, p.start, p.end, p.weekly_tss_target) for p in phases_a] \
        == [(p.name, p.start, p.end, p.weekly_tss_target) for p in phases_b]
    # Zero extra draws from the global RNG stream on the None path.
    assert rng_state_a == rng_state_b


def test_gb1_anchor_is_today_when_unset():
    # weeks_available anchors on today when start_date is None.
    g = tp.Goal(goal_type="general", target_date=ANCHOR + timedelta(days=70))
    assert g.weeks_available() == 10
    # And the phase split starts today.
    phases = tp.generate_phases(g, 50.0)
    assert phases[0].start == ANCHOR


def test_gb1_backdated_anchor_moves_split_start():
    start = ANCHOR - timedelta(days=21)
    g = tp.Goal(goal_type="general", target_date=ANCHOR + timedelta(days=70),
                start_date=start)
    # Full runway: 70 days remaining + 21 elapsed = 13 weeks.
    assert g.weeks_available() == 13
    phases = tp.generate_phases(g, 50.0)
    assert phases[0].start == start


# ── B-LOCKED-5 — _phase_start_override precedence ────────────────────────────

def test_override_present_start_date_ignored_by_splitter():
    start = ANCHOR - timedelta(days=28)
    target = ANCHOR + timedelta(days=70)
    g_legacy = tp.Goal(goal_type="general", target_date=target)
    g_entry = tp.Goal(goal_type="general", target_date=target, start_date=start)
    override = ANCHOR + timedelta(days=10)
    g_legacy._phase_start_override = override
    g_entry._phase_start_override = override

    assert g_legacy.weeks_available() == g_entry.weeks_available() == 10
    ph_legacy = tp.generate_phases(g_legacy, 50.0)
    ph_entry = tp.generate_phases(g_entry, 50.0)
    assert [(p.name, p.start, p.end, p.weekly_tss_target) for p in ph_legacy] \
        == [(p.name, p.start, p.end, p.weekly_tss_target) for p in ph_entry]
    assert ph_entry[0].start == override


# ── GB2 — backdated event goal (matrix pattern, single backdated cell) ──────

@pytest.fixture(scope="module")
def backdated_event():
    start = ANCHOR - timedelta(days=28)           # 4 full weeks in
    target = ANCHOR + timedelta(days=84)          # 12 more weeks to race day
    goal = tp.Goal(goal_type="event", target_date=target,
                   event_name="TestFondo", event_km=150.0,
                   event_climb_m=1500.0, start_date=start,
                   entry_mode="declared")
    phases, weeks = _gen(goal)
    return start, target, phases, weeks


def test_gb2_full_runway_split_and_position(backdated_event):
    start, target, phases, weeks = backdated_event
    # Split covers the FULL runway from the backdated start.
    assert phases[0].start == start
    # 16-week runway ⇒ full program shape incl. base (a today-anchored
    # 12-week runway would compress it away from a 4-phase full split).
    assert {p.name for p in phases} >= {"base", "build1", "taper"}
    # Contiguous week rows from start_date; union ends at the target.
    assert weeks[0].start == start
    for a, b in zip(weeks, weeks[1:]):
        assert (b.start - a.end).days == 1
    assert weeks[-1].end == target
    # Position: today falls in week floor((today-start)/7)+1 = 5.
    cur = [w for w in weeks if w.start <= ANCHOR <= w.end]
    assert len(cur) == 1 and cur[0].week_num == 5


def test_gb2_no_past_sessions_elapsed_rows_keep_tss(backdated_event):
    start, target, phases, weeks = backdated_event
    for w in weeks:
        if w.end < ANCHOR:                      # elapsed row
            assert w.sessions == []
            assert w.tss_target > 0             # planned-CTL annotators need it
        else:
            for s in w.sessions:
                assert s.day >= ANCHOR, "scheduled session in the past"
        assert w.tss_target > 0


def test_gb2_taper_race_guards_hold(backdated_event):
    start, target, phases, weeks = backdated_event
    # Taper anchored on target_date, unaffected by backdating.
    tapers = [p for p in phases if p.name == "taper"]
    assert len(tapers) == 1 and tapers[0].end == target
    assert tapers[0].start >= ANCHOR
    # No session past the target; race day marked ON the target; opener T-1.
    all_sessions = [s for w in weeks for s in w.sessions]
    assert max(s.day for s in all_sessions) <= target
    assert any(getattr(s, "is_race", False) and s.day == target for s in all_sessions)
    eve = [s for s in all_sessions if (target - s.day).days == 1]
    assert eve and any(getattr(s, "is_opener", False) for s in eve)


# ── B-LOCKED-2 — MODE 1 safety floor: remaining-weeks ramp credit ────────────

def test_ramp_clamp_remaining_weeks_only():
    """Zero-history claimer backdates 8 of 12 weeks ⇒ ramp credit spans the
    4 remaining (−2 taper buffer) weeks only — no 440-TSS build entry."""
    ctl = 35.0
    legacy = tp.Goal(goal_type="general", plan_weeks=12, hours_per_week=20.0)
    claimed = tp.Goal(goal_type="general", plan_weeks=12, hours_per_week=20.0,
                      start_date=ANCHOR - timedelta(days=56),
                      entry_mode="declared")
    max_legacy = max(p.weekly_tss_target
                     for p in tp.generate_phases(legacy, ctl))
    max_claimed = max(p.weekly_tss_target
                      for p in tp.generate_phases(claimed, ctl))
    assert max_claimed < max_legacy, "backdating must not inflate the ramp"
    assert max_claimed < 440, "no build2/440-TSS entry off a bare claim"
    # Exact ceiling: CTL + safe_ramp × (total − elapsed − 2), ×7 for weekly TSS.
    ramp = tp.safe_ramp_rate(ctl)
    assert max_claimed <= (ctl + ramp * (12 - 8 - 2)) * 7 + 1e-6


# ── input gate ───────────────────────────────────────────────────────────────

def test_future_start_date_rejected():
    g = tp.Goal(goal_type="general", plan_weeks=8,
                start_date=ANCHOR + timedelta(days=3))
    with pytest.raises(ValueError, match="future"):
        _gen(g)


def test_start_on_or_after_target_rejected():
    g = tp.Goal(goal_type="event", target_date=ANCHOR + timedelta(days=14),
                start_date=ANCHOR + timedelta(days=14) - timedelta(days=0))
    # start == target (both future-of-anchor? start must also be ≤ today —
    # use a past start ≥ target to hit the runway check unambiguously)
    g = tp.Goal(goal_type="event", target_date=ANCHOR - timedelta(days=7),
                start_date=ANCHOR - timedelta(days=7))
    with pytest.raises(ValueError):
        _gen(g)


# ── elapsed strip helper semantics ───────────────────────────────────────────

def test_strip_elapsed_sessions_entry_week_split():
    mk = lambda d: tp.PlannedSession(day=d, day_name=d.strftime("%a"),
                                     session_type="z2", duration_min=60,
                                     tss_estimate=45, description="")
    w_old = tp.PlannedWeek(week_num=1, start=ANCHOR - timedelta(days=14),
                           end=ANCHOR - timedelta(days=8), phase="base",
                           tss_target=300, is_stepback=False,
                           sessions=[mk(ANCHOR - timedelta(days=14))])
    w_cur = tp.PlannedWeek(week_num=2, start=ANCHOR - timedelta(days=3),
                           end=ANCHOR + timedelta(days=3), phase="base",
                           tss_target=300, is_stepback=False,
                           sessions=[mk(ANCHOR - timedelta(days=2)),
                                     mk(ANCHOR),
                                     mk(ANCHOR + timedelta(days=2))])
    weeks = [w_old, w_cur]
    tp._strip_elapsed_sessions(weeks, ANCHOR - timedelta(days=14))
    assert w_old.sessions == []
    assert [s.day for s in w_cur.sessions] == [ANCHOR, ANCHOR + timedelta(days=2)]
    assert w_old.tss_target == 300

    # None / today ⇒ strict no-op.
    w2 = tp.PlannedWeek(week_num=1, start=ANCHOR - timedelta(days=7),
                        end=ANCHOR - timedelta(days=1), phase="base",
                        tss_target=300, is_stepback=False,
                        sessions=[mk(ANCHOR - timedelta(days=7))])
    tp._strip_elapsed_sessions([w2], None)
    assert len(w2.sessions) == 1
    tp._strip_elapsed_sessions([w2], ANCHOR)
    assert len(w2.sessions) == 1


# ── FTP-test injection → first schedulable week ──────────────────────────────

def test_ftp_test_retargets_to_first_schedulable_week(backdated_event):
    """The build2-start FTP test must never be lost to the elapsed strip; on
    a backdated plan whose test week is in the past it lands in the first
    schedulable week instead (recalibration AT entry)."""
    start, target, phases, weeks = backdated_event
    tests = [(w, s) for w in weeks for s in w.sessions
             if s.session_type == "ftp_test"]
    build2 = next((p for p in phases if p.name == "build2"), None)
    if build2 is not None and build2.start < ANCHOR:
        assert tests, "elapsed build2 start must retarget, not drop, the test"
    for w, s in tests:
        assert s.day >= ANCHOR


def test_ftp_test_retarget_unit():
    mk = lambda d, t: tp.PlannedSession(day=d, day_name=d.strftime("%a"),
                                        session_type=t, duration_min=60,
                                        tss_estimate=60, description="")
    # Elapsed build2 phase (started 2 weeks ago) + a schedulable entry week.
    ph = [tp.Phase(name="build2", start=ANCHOR - timedelta(days=14),
                   end=ANCHOR + timedelta(days=13), weeks=4, focus="",
                   weekly_tss_target=400, z2_pct=70, hit_per_week=2,
                   session_types=["threshold"])]
    w_elapsed = tp.PlannedWeek(week_num=1, start=ANCHOR - timedelta(days=14),
                               end=ANCHOR - timedelta(days=8), phase="build2",
                               tss_target=400, is_stepback=False, sessions=[])
    w_entry = tp.PlannedWeek(week_num=2, start=ANCHOR - timedelta(days=7),
                             end=ANCHOR - timedelta(days=1), phase="build2",
                             tss_target=400, is_stepback=False,
                             sessions=[mk(ANCHOR - timedelta(days=3), "threshold")])
    w_next = tp.PlannedWeek(week_num=3, start=ANCHOR, end=ANCHOR + timedelta(days=6),
                            phase="build2", tss_target=400, is_stepback=False,
                            sessions=[mk(ANCHOR + timedelta(days=1), "threshold")])
    tp._inject_mid_cycle_ftp_tests([w_elapsed, w_entry, w_next], ph)
    # w_entry ends before today → not schedulable; w_next gets the test, on a
    # today-or-later slot only.
    types_next = [s.session_type for s in w_next.sessions]
    assert "ftp_test" in types_next
    assert all(s.session_type != "ftp_test" for s in w_entry.sessions)


# ── GB4 — ICU push window never includes elapsed weeks ──────────────────────

def test_gb4_push_window_excludes_elapsed_and_past(backdated_event, tmp_path, monkeypatch):
    """Even against an ADVERSARIAL plan dict (a stray session parked in an
    elapsed week), _desired_events' G-H window starts today: nothing before
    ANCHOR may surface as an event, a skip, or a broken id."""
    import icu_calendar_push as icp
    import app as app_module

    start, target, phases, weeks = backdated_event
    plan = {"weeks": json.loads(_serialize_weeks(weeks).decode())}
    # Adversarial stray: hand-inject a past-dated session into an elapsed row.
    plan["weeks"][0]["sessions"] = [{
        "day": (ANCHOR - timedelta(days=21)).isoformat(),
        "session_type": "endurance", "zwo_file": "ghost.zwo",
        "duration_min": 60, "tss_estimate": 55,
    }]

    class _PM:  # minimal surface _desired_events touches
        _athlete = {"target_mode": "power"}
        lthr_is_set = False
        max_hr = 190
        lthr = 170

    monkeypatch.setattr(app_module, "WORKOUT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(icp, "_load_classifications", lambda _d: {})

    events, skipped, broken = icp._desired_events(
        _PM(), plan, ANCHOR, 14, "prof1")
    today_iso = ANCHOR.isoformat()
    for ev in events:
        assert ev["start_date_local"][:10] >= today_iso
    for sk in skipped:
        assert sk["day"] >= today_iso, f"pre-entry day leaked into skips: {sk}"
    for bid in broken:
        assert bid.split(":")[2] >= today_iso


# ── GB5 — no missed-suggestion / refit storm at entry ───────────────────────

def test_gb5_no_missed_storm_at_entry(backdated_event):
    """Freshly generated backdated plan: 4 elapsed weeks must yield ZERO
    missed suggestions and ZERO missed-hard candidates (elapsed rows carry no
    sessions, so nothing reads as 'missed history')."""
    import app as app_module

    start, target, phases, weeks = backdated_event
    plan = {"weeks": json.loads(_serialize_weeks(weeks).decode())}

    sugg = app_module._compute_missed_suggestions(plan, ANCHOR)
    assert sugg == []

    # Missed-hard scan precondition (app:11867 inline): a session only counts
    # with status == "missed". A fresh backdated plan has no statuses at all.
    assert not any(
        s.get("status") == "missed"
        for w in plan["weeks"] for s in w.get("sessions", [])
    )


# ── Persistence round-trip — the anchor survives serialize → parse ──────────

def test_start_date_roundtrip_through_plan_dict():
    """Write-shape (api generate, app:10826) → _goal_from_plan_dict
    (app:11560) → Goal: start_date + entry_mode survive; absent fields parse
    to None (legacy plans untouched)."""
    import app as app_module

    g = {"type": "event", "target_date": "2026-04-01",
         "start_date": "2026-01-01", "entry_mode": "declared",
         "event_km": 150, "event_climb": 1500}
    goal = app_module._goal_from_plan_dict(g)
    assert goal.start_date == date(2026, 1, 1)
    assert goal.entry_mode == "declared"

    legacy = app_module._goal_from_plan_dict(
        {"type": "ftp", "target_date": "2026-04-01"})
    assert legacy.start_date is None and legacy.entry_mode is None

    # tp-side reconstructors carry the fields too (reforecast tp:8553,
    # recovery refit tp:8988, weekly recalc tp:9464) — pinned here at the
    # Goal level: a Goal round-trips through its own field set.
    src = tp.Goal(goal_type="event", target_date=date(2026, 4, 1),
                  start_date=date(2026, 1, 1), entry_mode="declared")
    clone = tp.Goal(goal_type=src.goal_type, target_date=src.target_date,
                    start_date=getattr(src, "start_date", None),
                    entry_mode=getattr(src, "entry_mode", None))
    assert clone.start_date == src.start_date
    assert clone.entry_mode == src.entry_mode


# ══ GP5 (v3.2.0 phase-split editor) — backdated + custom split ═══════════════
# GB2/GB4/GB5 patterns re-run with phase_weeks set, including the
# elapsed>custom-base edge: 4 weeks already elapsed but the custom base is
# only 3 — entry lands mid-build1; position math, elapsed-strip, race guards
# and the ICU push window must all hold unchanged.

GP5_CUSTOM = {"base": 3, "build1": 4, "build2": 4, "peak": 3, "taper": 2}


@pytest.fixture(scope="module")
def backdated_custom_event():
    start = ANCHOR - timedelta(days=28)           # 4 full weeks in (> base 3)
    target = ANCHOR + timedelta(days=84)          # 12 more weeks to race day
    goal = tp.Goal(goal_type="event", target_date=target,
                   event_name="TestFondo", event_km=150.0,
                   event_climb_m=1500.0, start_date=start,
                   entry_mode="declared", phase_weeks=dict(GP5_CUSTOM))
    phases, weeks = _gen(goal)
    return start, target, goal, phases, weeks


def test_gp5_custom_split_applied_over_full_runway(backdated_custom_event):
    start, target, goal, phases, weeks = backdated_custom_event
    assert goal._phase_weeks_status == "applied"
    assert phases[0].start == start
    by_name = {p.name: p for p in phases}
    for name in ("base", "build1", "build2", "taper"):
        assert by_name[name].weeks == GP5_CUSTOM[name], name
    peak_span = (by_name["peak"].end - by_name["peak"].start).days + 1
    assert abs(peak_span - 7 * GP5_CUSTOM["peak"]) <= 6   # A4 absorber
    for a, b in zip(phases, phases[1:]):
        assert (b.start - a.end).days == 1
    # Position: today is week 5; base is only 3 weeks, so entry lands
    # mid-build1 (the elapsed>custom-base edge).
    cur = [w for w in weeks if w.start <= ANCHOR <= w.end]
    assert len(cur) == 1 and cur[0].week_num == 5
    assert cur[0].phase == "build1"
    assert weeks[0].start == start and weeks[-1].end == target


def test_gp5_no_past_sessions_elapsed_rows_keep_tss(backdated_custom_event):
    start, target, goal, phases, weeks = backdated_custom_event
    for w in weeks:
        if w.end < ANCHOR:                      # elapsed row
            assert w.sessions == []
        else:
            for s in w.sessions:
                assert s.day >= ANCHOR, "scheduled session in the past"
        assert w.tss_target > 0


def test_gp5_taper_race_guards_hold(backdated_custom_event):
    start, target, goal, phases, weeks = backdated_custom_event
    tapers = [p for p in phases if p.name == "taper"]
    assert len(tapers) == 1 and tapers[0].end == target
    # Custom taper 2 ⇒ exactly 14 days to the target (A4 taper-to-target).
    assert (tapers[0].end - tapers[0].start).days + 1 == 14
    all_sessions = [s for w in weeks for s in w.sessions]
    assert max(s.day for s in all_sessions) <= target
    assert any(getattr(s, "is_race", False) and s.day == target
               for s in all_sessions)
    eve = [s for s in all_sessions if (target - s.day).days == 1]
    assert eve and any(getattr(s, "is_opener", False) for s in eve)


def test_gp5_gb4_push_window_excludes_elapsed(backdated_custom_event,
                                              tmp_path, monkeypatch):
    import icu_calendar_push as icp
    import app as app_module

    start, target, goal, phases, weeks = backdated_custom_event
    plan = {"weeks": json.loads(_serialize_weeks(weeks).decode())}
    plan["weeks"][0]["sessions"] = [{
        "day": (ANCHOR - timedelta(days=21)).isoformat(),
        "session_type": "endurance", "zwo_file": "ghost.zwo",
        "duration_min": 60, "tss_estimate": 55,
    }]

    class _PM:
        _athlete = {"target_mode": "power"}
        lthr_is_set = False
        max_hr = 190
        lthr = 170

    monkeypatch.setattr(app_module, "WORKOUT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(icp, "_load_classifications", lambda _d: {})

    events, skipped, broken = icp._desired_events(
        _PM(), plan, ANCHOR, 14, "prof1")
    today_iso = ANCHOR.isoformat()
    for ev in events:
        assert ev["start_date_local"][:10] >= today_iso
    for sk in skipped:
        assert sk["day"] >= today_iso
    for bid in broken:
        assert bid.split(":")[2] >= today_iso


def test_gp5_gb5_no_missed_storm(backdated_custom_event):
    import app as app_module

    start, target, goal, phases, weeks = backdated_custom_event
    plan = {"weeks": json.loads(_serialize_weeks(weeks).decode())}
    assert app_module._compute_missed_suggestions(plan, ANCHOR) == []
    assert not any(
        s.get("status") == "missed"
        for w in plan["weeks"] for s in w.get("sessions", [])
    )


def test_gp5_phase_weeks_roundtrip_through_plan_dict():
    """Write-shape (goal block) → _goal_from_plan_dict → Goal.phase_weeks;
    absent field parses to None (legacy plans untouched)."""
    import app as app_module

    g = {"type": "event", "event_date": "2026-04-01",
         "phase_weeks": {"base": 3, "build1": 4, "build2": 4,
                         "peak": 3, "taper": 2}}
    goal = app_module._goal_from_plan_dict(g)
    assert goal.phase_weeks == g["phase_weeks"]

    legacy = app_module._goal_from_plan_dict({"type": "ftp"})
    assert legacy.phase_weeks is None
