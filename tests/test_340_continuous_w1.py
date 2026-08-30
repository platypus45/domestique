"""3.4.0 W1 (IP_CONTINUOUS_MODE amendments A+B) — continuous goal engine.

A — goal_type "continuous" (focus prefs ftp|vo2|both): rolling
    CONTINUOUS_HORIZON_WEEKS horizon, ONE "continuous" phase, 3-load:1-deload
    via the existing stepback cadence, NO taper/consolidation (the 3.3.2
    goal_type taper gate extended to continuous by exclusion).
B — recalc EXTEND path: extend_continuous_plan drops elapsed weeks from the
    horizon and appends week N+4 (kept weeks byte-identical, pinned-seed
    deterministic); the 3.3.1 pool-collapse breaker guards the append; the
    P1 finite-plan engine consumers take plan_weeks=None / continuous
    without crashing (complete no-target readiness shape).

Hermetic: pinned current_ctl + recent_weekly_tss on every engine call, no
network (conftest gates), and the tracked workouts/.library_index.json is
snapshotted + restored around the module (the planner rewrites it on load).
"""
import copy
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pytest

import training_planner as tp

_LIB_INDEX = Path(__file__).resolve().parent.parent / "workouts" / ".library_index.json"

# Pinned engine inputs (hermetic sizing — no archive/ICU self-fetch).
CTL = 50.0
RWT = 500.0
HIT_TYPES = {"vo2max", "threshold", "overunder", "sweetspot", "sprint", "tempo"}


@pytest.fixture(scope="module", autouse=True)
def _restore_library_index():
    backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None
    yield
    if backup is not None:
        _LIB_INDEX.write_bytes(backup)


def _goal(focus="both", **kw):
    return tp.Goal(goal_type="continuous", target_date=None,
                   hours_per_week=8.0, focus=focus, **kw)


def _generate(goal):
    return tp.generate_plan(goal, current_ctl=CTL, recent_weekly_tss=RWT)


def _shift(weeks, days):
    """Move a generated plan back in time to simulate elapsed weeks."""
    for w in weeks:
        w.start += timedelta(days=days)
        w.end += timedelta(days=days)
        for s in w.sessions:
            s.day += timedelta(days=days)
    return weeks


def _sig(week):
    """Byte-level-ish session signature for kept-week immutability checks."""
    return [(s.day.isoformat(), s.session_type, s.zwo_file, s.duration_min,
             s.tss_estimate) for s in week.sessions]


# ── A: continuous generate — rolling horizon, no taper ─────────────────────

def test_continuous_generates_rolling_horizon_no_taper():
    goal = _goal()
    assert goal.weeks_available() == tp.CONTINUOUS_HORIZON_WEEKS == 4
    phases, weeks = _generate(goal)

    # ONE rolling block, no taper/consolidation/base/build/peak anywhere.
    assert [p.name for p in phases] == ["continuous"]
    assert all(p.name not in ("taper", "consolidation") for p in phases)
    assert len(weeks) == 4
    assert all(w.phase == "continuous" for w in weeks)

    # 3 load : 1 deload — the deload rides the stepback cadence at W4.
    assert [w.is_stepback for w in weeks] == [False, False, False, True]
    assert weeks[3].tss_target < weeks[0].tss_target  # ×0.72 discount
    assert not any(s.session_type in HIT_TYPES for s in weeks[3].sessions), \
        "deload week must carry no HIT session"
    # Load weeks actually train (the horizon isn't a Z2 skeleton).
    assert any(s.session_type in HIT_TYPES
               for w in weeks[:3] for s in w.sessions)


@pytest.mark.parametrize("focus,profile", [
    ("ftp", "ftp"), ("vo2", "vo2max"), ("both", "ftp_vo2max")])
def test_continuous_focus_prefs(focus, profile):
    goal = _goal(focus=focus)
    # The pref maps onto an EXISTING sampler emphasis profile.
    assert tp._continuous_emphasis(goal) == profile
    assert profile in tp.GOAL_CLASS_EMPHASIS
    phases, weeks = _generate(goal)
    assert len(weeks) == 4 and phases[0].name == "continuous"


def test_continuous_ignores_finite_plan_fields():
    # plan_weeks / phase_weeks are finite-plan controls: the horizon stays
    # rolling and the phase-split editor is disabled (P1 items 1-3).
    goal = _goal(plan_weeks=24, phase_weeks={"base": 4})
    assert goal.weeks_available() == 4
    phases, weeks = _generate(goal)
    assert len(weeks) == 4
    assert str(getattr(goal, "_phase_weeks_status", "")).startswith("fallback:")
    rec, reason = tp._recommended_phase_weeks(_goal())
    assert rec is None and reason
    vec, vreason = tp.validate_phase_weeks(_goal(), {"base": 4})
    assert vec is None and vreason


# ── B: weekly recalc EXTENDS (drop elapsed, append week N+4) ───────────────

def test_extend_appends_week_and_keeps_existing_weeks():
    goal = _goal()
    _, weeks = _generate(goal)
    weeks = _shift(weeks, -7)  # one week elapsed
    kept_before = [_sig(w) for w in weeks]
    old_last_end = weeks[-1].end

    new_phases, all_weeks, info = tp.extend_continuous_plan(
        goal, copy.deepcopy(weeks), current_ctl=CTL, recent_weekly_tss=RWT)

    assert info["action"] == "extended"
    assert info["weeks_appended"] == 1
    assert info["taper_locked"] is False
    assert len(all_weeks) == 5

    appended = all_weeks[-1]
    assert appended.week_num == 5
    assert appended.start == old_last_end + timedelta(days=1)  # contiguous
    assert appended.phase == "continuous"
    assert not appended.is_stepback  # W5 is a load week (5 % 4 != 0)
    assert any(s.session_type != "rest" for s in appended.sessions)

    # Kept weeks (past + future) byte-identical — extend never rewrites them.
    assert [_sig(w) for w in all_weeks[:4]] == kept_before

    # The returned rolling phase covers the ahead window + append, no taper.
    assert [p.name for p in new_phases] == ["continuous"]
    assert new_phases[0].end == appended.end


def test_extend_is_deterministic_pinned_seeds():
    goal = _goal(focus="ftp")
    _, weeks = _generate(goal)
    weeks = _shift(weeks, -7)
    _, all1, info1 = tp.extend_continuous_plan(
        goal, copy.deepcopy(weeks), current_ctl=CTL, recent_weekly_tss=RWT)
    _, all2, info2 = tp.extend_continuous_plan(
        goal, copy.deepcopy(weeks), current_ctl=CTL, recent_weekly_tss=RWT)
    assert info1["action"] == info2["action"] == "extended"
    assert _sig(all1[-1]) == _sig(all2[-1]), \
        "same inputs must append the same week (pinned-seed contract)"


def test_extend_full_horizon_deficit_keeps_deload_cadence():
    goal = _goal()
    _, weeks = _generate(goal)
    weeks = _shift(weeks, -28)  # whole horizon elapsed → deficit 4
    _, all_weeks, info = tp.extend_continuous_plan(
        goal, copy.deepcopy(weeks), current_ctl=CTL, recent_weekly_tss=RWT)
    assert info["action"] == "extended" and info["weeks_appended"] == 4
    appended = all_weeks[-4:]
    assert [w.week_num for w in appended] == [5, 6, 7, 8]
    # 3-load:1-deload continues positionally: W8 is the next deload.
    assert [w.is_stepback for w in appended] == [False, False, False, True]
    assert appended[3].tss_target < appended[0].tss_target
    # FTP-tests W1e retest cadence (weeks-since-last-test, not week_num % 6):
    # the continuous generate path baselines the rider with a week-2 test, so
    # the next test is due at W8 — a deload — and DEFERS to the next appended
    # non-deload week instead of landing on tired legs or (the old %6 bug)
    # silently vanishing into a 12-week hole. No test in this batch is the
    # correct behaviour; the base plan must carry the week-2 baseline.
    base_weeks = all_weeks[:-4]
    assert any(s.session_type == "ftp_test"
               for w in base_weeks for s in w.sessions), \
        "continuous generation must baseline with a week-2 FTP test"
    assert not any(s.session_type == "ftp_test"
                   for w in appended for s in w.sessions)
    # The appended horizon serves today onward (no dead past weeks).
    assert appended[-1].end >= date.today()


def test_extend_no_change_when_horizon_full():
    goal = _goal()
    _, weeks = _generate(goal)  # all 4 weeks still ahead
    _, all_weeks, info = tp.extend_continuous_plan(
        goal, copy.deepcopy(weeks), current_ctl=CTL, recent_weekly_tss=RWT)
    assert info["action"] == "no_change"
    assert info["reason"] == "horizon_full"
    assert len(all_weeks) == 4

    # Nothing to extend at all → explicit no_plan, not a crash.
    _, _, info2 = tp.extend_continuous_plan(
        goal, [], current_ctl=CTL, recent_weekly_tss=RWT)
    assert info2["action"] == "no_change" and info2["reason"] == "no_plan"


def test_recalculate_plan_routes_continuous_to_extend():
    """The 3.3.2 taper gate, extended: a continuous recalc can never
    taper-lock or regenerate-to-target — it extends via the rolling path,
    even through the legacy recalculate_plan entrypoint."""
    goal = _goal()
    _, weeks = _generate(goal)
    weeks = _shift(weeks, -7)
    new_phases, all_weeks, info = tp.recalculate_plan(
        goal, copy.deepcopy(weeks), current_ctl=CTL)
    assert info["action"] == "extended"
    assert info.get("taper_locked") is False
    assert all(p.name != "taper" for p in new_phases)
    assert all(w.phase != "taper" for w in all_weeks)


# ── B: pool-collapse breaker covers the append ─────────────────────────────

def test_extend_breaker_aborts_append_on_empty_pools():
    goal = _goal()
    _, weeks = _generate(goal)
    weeks = _shift(weeks, -7)
    kept_before = [_sig(w) for w in weeks]
    # A non-trivial (≥100 file) library whose admissible pool is empty is the
    # 3.3.0 storm signature — the breaker must abort the append.
    junk = [{"File": f"junk_{i}.zwo", "Name": f"junk {i}", "Score": 0}
            for i in range(150)]
    with mock.patch.object(tp, "load_workout_library", return_value=junk):
        _, all_weeks, info = tp.extend_continuous_plan(
            goal, copy.deepcopy(weeks), current_ctl=CTL, recent_weekly_tss=RWT)
    assert info["action"] == "no_change"
    assert info["reason"] == "pool_collapse"
    assert len(all_weeks) == 4, "no week may be appended on a collapsed pool"
    assert [_sig(w) for w in all_weeks] == kept_before


# ── P1: finite-plan engine consumers survive plan_weeks=None/continuous ────

def test_event_readiness_no_target_returns_complete_shape():
    for gtype in ("continuous", "general", "ftp"):
        r = tp.compute_event_readiness(
            tp.Goal(goal_type=gtype, target_date=None), 48.0)
        assert r["status"] == "no_event"
        assert r["weeks_remaining"] is None
        # The keys recalculate_plan indexes directly (the old 2-key stub
        # KeyError'd every no-target caller — P1 item 6).
        assert r["pct_of_target"] == 100.0
        assert r["taper_action"] == "none" and r["taper_days"] == 0
        assert r["ramp_feasible"] is True


def test_weeks_available_survives_plan_weeks_none():
    g = tp.Goal(goal_type="general", target_date=None)
    g.plan_weeks = None
    assert g.weeks_available() == 16
    gc = _goal()
    gc.plan_weeks = None
    assert gc.weeks_available() == 4


def test_finite_plan_consumers_dont_crash_on_continuous():
    goal = _goal()
    # Entry recognizer (P1 item 14/12): continuous is non-event, the
    # MIN_REMAINING_WEEKS reservation zeroes the candidate range → fresh start.
    res = tp.recognize_entry(goal, [], current_ctl=CTL)
    assert res["proposal_weeks"] == 0
    assert res["weeks_remaining"] == tp.CONTINUOUS_HORIZON_WEEKS

    # Budget lookup maps the continuous block onto build1 under EVERY model
    # (P1: get_budget_for_phase must not fall back to the base budget).
    try:
        for model in ("polarized", "pyramidal", "threshold"):
            tp.set_active_distribution(model)
            assert tp.get_budget_for_phase("continuous") is \
                tp.get_budget_for_phase("build1")
    finally:
        tp.set_active_distribution("polarized")

    # Recovery/gap rebuild (P1 items 8/11): regenerate_from_today on a
    # continuous plan re-emits the rolling block, never a taper.
    _, weeks = _generate(goal)
    new_phases, all_weeks, _info = tp.regenerate_from_today(
        goal, weeks, CTL)
    assert {p.name for p in new_phases} == {"continuous"}

    # recalculate_plan with a NO-target finite goal and a fully-elapsed plan
    # exercises the None-target readiness guard end-to-end (P1 items 6-7).
    g5 = tp.Goal(goal_type="general", target_date=None, hours_per_week=8.0)
    _, wk5 = tp.generate_plan(g5, current_ctl=CTL, recent_weekly_tss=RWT)
    _shift(wk5, -400)
    _, _, info5 = tp.recalculate_plan(g5, wk5, current_ctl=CTL)
    assert info5["action"] in ("recalculated", "no_change")
