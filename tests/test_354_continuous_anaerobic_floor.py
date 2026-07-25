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


def test_anaerobic_survives_the_weekly_extend_recalc():
    """The owner's hard requirement: the guarantee must survive the dynamic
    weekly rolling recalc (extend_continuous_plan), not just fresh generation.
    Generate, then extend, and assert the rolling window still carries the
    anaerobic/neuromuscular floor rather than eroding to zero."""
    goal = _goal()
    _p, weeks = tp.generate_plan(goal, current_ctl=CTL, recent_weekly_tss=RWT)
    # Roll the plan forward a few weeks via the same path the app uses.
    for _ in range(4):
        _phases, weeks, _info = tp.recalculate_plan(
            goal, weeks, current_ctl=CTL, recent_activities=[])
    # Over the most recent 4-week window, anaerobic+neuromuscular must not be
    # zero (the floor is enforced per rolling block, counting the kept window).
    recent = sorted(weeks, key=lambda w: w.start)[-4:]
    c = _class_counts(recent)
    assert (c.get("anaerobic", 0) + c.get("neuromuscular", 0)) >= 1, (
        f"anaerobic/neuromuscular eroded to zero after weekly extends: {dict(c)}")
