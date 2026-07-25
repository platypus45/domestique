"""v3.5.4 — continuous plans must schedule anaerobic + neuromuscular work.

Root cause fixed here: `phase_floors` (the only mechanism that GUARANTEES a
workout class appears) was keyed build1/build2/peak and never matched the
single "continuous" phase, so 58% of fresh continuous plans had ZERO
anaerobic AND ZERO neuromuscular despite the owner explicitly wanting sprint /
anaerobic-capacity work. Also pins the companion deload-cleanliness fix
(Finding 6): a stepback week must carry no genuinely-hard session.
"""
from __future__ import annotations

import collections
from datetime import timedelta
from pathlib import Path

import pytest

import training_planner as tp

CTL = 50.0
RWT = 500.0
_LIB_INDEX = Path(__file__).resolve().parent.parent / "workouts" / ".library_index.json"

# Session types the deload week must not contain (Issurin unloading). Mirrors
# tests/test_340_continuous_w1.py::HIT_TYPES but drops "tempo": the planner
# deliberately allows a low-TSS easy-tempo spin in a stepback week
# (_HIT_SESSION_TYPES excludes tempo), and that is out of scope for Finding 6,
# which was the genuinely-hard leak (a 100-min/TSS-103 sweet-spot session).
_HARD_TYPES = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}


@pytest.fixture(scope="module", autouse=True)
def _restore_library_index():
    backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None
    yield
    if backup is not None:
        _LIB_INDEX.write_bytes(backup)


def _goal(**kw):
    return tp.Goal(goal_type="continuous", target_date=None,
                   hours_per_week=8.0, focus="both", **kw)


def _class_counts(weeks):
    c: collections.Counter = collections.Counter()
    for w in weeks:
        for s in w.sessions:
            if s.session_type == "rest":
                continue
            c[tp._content_class_for_zwo(s.zwo_file or "")] += 1
    return c


def test_continuous_phase_floor_declares_anaerobic_and_neuromuscular():
    # The floor table must carry a "continuous" row — this is the fix.
    # Reach it the same way generate_plan does: the dict is function-local, so
    # assert on observed output rather than the literal (below), but also pin
    # that the classes are the intended ones via a fresh plan.
    goal = _goal()
    phases, weeks = tp.generate_plan(goal, current_ctl=CTL, recent_weekly_tss=RWT)
    assert [p.name for p in phases] == ["continuous"]
    c = _class_counts(weeks)
    assert c.get("anaerobic", 0) >= 1, f"no anaerobic in continuous plan: {dict(c)}"
    assert c.get("neuromuscular", 0) >= 1, f"no neuromuscular in continuous plan: {dict(c)}"


def test_continuous_anaerobic_guarantee_holds_across_seeds():
    """Was 58% zero-both before the fix. Must be 0% now."""
    zero_both = 0
    for seed in range(24):
        goal = _goal()
        _p, weeks = tp.generate_plan(
            goal, current_ctl=CTL, recent_weekly_tss=RWT, seed_salt=str(seed))
        c = _class_counts(weeks)
        if c.get("anaerobic", 0) == 0 and c.get("neuromuscular", 0) == 0:
            zero_both += 1
    assert zero_both == 0, f"{zero_both}/24 continuous plans still had zero anaerobic+neuromuscular"


def test_deload_week_carries_no_hard_session():
    """Finding 6: a stepback week drew a 100-min sweet-spot ride (TSS 103) into
    a week targeting 268 TSS. No genuinely-hard session may land in a deload."""
    for seed in range(24):
        goal = _goal()
        _p, weeks = tp.generate_plan(
            goal, current_ctl=CTL, recent_weekly_tss=RWT, seed_salt=str(seed))
        for w in weeks:
            if not getattr(w, "is_stepback", False):
                continue
            hard = [(s.session_type, round(s.tss_estimate), s.zwo_file)
                    for s in w.sessions if s.session_type in _HARD_TYPES]
            assert not hard, f"seed {seed} deload wk{w.week_num} carried hard work: {hard}"


def _elapse_one_week(weeks):
    """Shift every week back 7 days == one real week passing. The ONLY way to
    make extend_continuous_plan actually append; without this the horizon is
    already full and recalc returns "no_change", so the floor never runs and a
    test can pass while the guarantee is broken."""
    for w in weeks:
        w.start -= timedelta(days=7)
        w.end -= timedelta(days=7)
        for s in w.sessions:
            if getattr(s, "day", None):
                s.day -= timedelta(days=7)
    return weeks


def test_anaerobic_cadence_sustains_across_rolling_weeks():
    """The owner's hard requirement: the guarantee must SURVIVE the dynamic
    weekly rolling recalc indefinitely — not fire once and then go dry.

    Regression pin for the trailing-window defect: the count window was
    `current_plan_weeks + new_weeks` (the whole plan HISTORY), so one anaerobic
    session in any past week satisfied the floor forever. Measured on the
    owner's real plan over 14 elapsed weeks: 1 new anaerobic total, and 11
    consecutive weeks where he had zero anaerobic scheduled ahead of him.
    With the trailing window it is 4 — a clean ~4-week cadence.
    """
    goal = _goal()
    _p, weeks = tp.generate_plan(goal, current_ctl=CTL, recent_weekly_tss=RWT)
    baseline = _class_counts(weeks)
    new_ana = new_nm = 0
    for _ in range(12):
        weeks = _elapse_one_week(weeks)
        before = _class_counts(weeks)
        _phases, weeks, _info = tp.recalculate_plan(
            goal, weeks, current_ctl=CTL, recent_activities=[])
        after = _class_counts(weeks)
        new_ana += max(0, after.get("anaerobic", 0) - before.get("anaerobic", 0))
        new_nm += max(0, after.get("neuromuscular", 0) - before.get("neuromuscular", 0))
    # Over 12 rolling weeks the floor must keep re-firing as exposures age out
    # of the window. Pre-fix this was 1 (fired once, then never). Assert >=2 so
    # the test proves RE-firing, not just the first fill, without pinning an
    # exact cadence the sampler is free to vary.
    assert new_ana >= 2, (
        f"anaerobic fired {new_ana}x over 12 rolling weeks — the floor is not "
        f"re-firing as exposures age out (baseline plan: {dict(baseline)})")
    assert new_nm >= 2, f"neuromuscular fired only {new_nm}x over 12 rolling weeks"


def test_rolling_window_is_trailing_not_whole_history():
    """Pin the window semantics directly: the count window handed to the floor
    must be at most CONTINUOUS_HORIZON_WEEKS long, never the whole plan."""
    src = Path(tp.__file__).read_text()
    block = src[src.index("_keep_n = max(0, CONTINUOUS_HORIZON_WEEKS"):]
    block = block[:block.index("_enforce_build2_peak_hard_floor")]
    # Trailing slice, and the [-0:] whole-list trap explicitly guarded.
    assert "_kept_sorted[-_keep_n:] if _keep_n > 0 else []" in block
    assert "key=lambda w: w.start" in block, "kept weeks must be sorted"
    assert "(current_plan_weeks or []) + new_weeks" not in block, (
        "count window regressed to the whole plan history")
