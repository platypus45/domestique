"""v2.0.3 F6 (T-RECALC-MIX) — recalculate_plan routes weeks through the sampler.

Before F6, ``recalculate_plan`` built future weeks via ``plan_week`` (the legacy
structural skeleton) + ``match_zwo`` only — it never called the content-aware
``sample_week_workouts``. So mix-emphasis and the over_under hard-floor (F1)
could never follow a weekly recalc, and the plan diverged from first
generation. F6 routes recalc's week construction through ``sample_week_workouts``
the same way generate_plan / regenerate_from_today do.

These tests prove the routing took effect:
  1. The recalc's build-phase mix is content-classified across MANY distinct
     interval shapes (a plan_week-only skeleton produces a far narrower,
     filename-driven set) — i.e. the recalc shape matches first generation.
  2. The over_under hard-floor reaches the recalc (build1+build2 contain
     over_under), which is only possible via the sampler + floor pass.
  3. The endpoint contract is unchanged: recalc still returns the
     (phases, all_weeks, info) 3-tuple with a valid plan.
"""
from datetime import date, timedelta

import pytest

import training_planner as tp


_INTERVAL_CCS = {
    "sweet_spot", "threshold", "vo2max", "vo2_short",
    "over_under", "anaerobic", "neuromuscular",
}
_INTERVAL_FLAGS = (
    "has_threshold_work", "has_vo2_work", "has_sprints",
    "has_sweet_spot_work", "pattern_over_under", "pattern_microinterval",
)
_ATHLETE = {"ftp": 250, "weight_kg": 70}


def _classify(zwo_file: str) -> tuple[str, bool]:
    cache = tp._load_content_classifications()
    if not zwo_file:
        return "", False
    ent = cache.get(zwo_file) or cache.get(zwo_file.split("/")[-1])
    if not ent:
        return "", False
    cc = (ent.get("primary") or "").lower()
    if cc in _INTERVAL_CCS:
        return cc, True
    if cc == "mixed":
        flags = ent.get("secondary_flags") or {}
        return cc, any(flags.get(f, False) for f in _INTERVAL_FLAGS)
    return cc, False


def _event_goal() -> tp.Goal:
    return tp.Goal(
        goal_type="event",
        plan_weeks=20,
        target_date=date.today() + timedelta(weeks=20),
        event_km=160,
        event_climb_m=2000,
        event_type="gran_fondo",
        hours_per_week=10.0,
        max_weekday_hours=2.0,
        max_weekend_hours=5.0,
        available_days=[1, 2, 3, 4, 5, 6],
        rest_days=[0],
    )


def _build_shapes(weeks, phases=("build1", "build2")) -> set:
    shapes: set = set()
    for w in weeks:
        if w.phase not in phases:
            continue
        for s in w.sessions:
            if s.session_type == "rest":
                continue
            cc, is_intvl = _classify(s.zwo_file or "")
            if is_intvl:
                shapes.add(cc)
    return shapes


@pytest.fixture(scope="module")
def recalc_result():
    goal = _event_goal()
    phases, weeks = tp.generate_plan(goal, athlete=_ATHLETE)
    new_phases, all_weeks, info = tp.recalculate_plan(
        goal, weeks, current_ctl=42.0, athlete=_ATHLETE,
    )
    return goal, weeks, new_phases, all_weeks, info


def test_recalc_returns_valid_tuple_contract(recalc_result):
    """Endpoint contract: (phases:list, all_weeks:list[PlannedWeek], info:dict)."""
    _goal, _gen, new_phases, all_weeks, info = recalc_result
    assert isinstance(new_phases, list)
    assert isinstance(all_weeks, list) and all_weeks
    assert all(isinstance(w, tp.PlannedWeek) for w in all_weeks)
    assert isinstance(info, dict)
    assert info.get("action") in ("recalculated", "no_change")


def test_recalc_build_mix_is_content_aware(recalc_result):
    """Routing through the sampler yields a broad, content-classified build mix
    (≥4 distinct interval shapes) — a plan_week-only skeleton could not."""
    _goal, _gen, _np, all_weeks, info = recalc_result
    if info.get("action") == "no_change":
        pytest.skip("recalc was a no-op for this CTL — nothing routed")
    shapes = _build_shapes(all_weeks)
    assert len(shapes) >= 4, (
        f"recalc build mix too narrow ({sorted(shapes)}) — sampler routing "
        f"(F6) did not take effect"
    )


def test_recalc_over_under_floor_reaches_recalc(recalc_result):
    """The over_under hard-floor (F1) reaches the recalc — only possible once
    recalc routes through sample_week_workouts + the floor pass (F6)."""
    _goal, _gen, _np, all_weeks, info = recalc_result
    if info.get("action") == "no_change":
        pytest.skip("recalc was a no-op for this CTL — nothing routed")
    shapes = _build_shapes(all_weeks)
    assert "over_under" in shapes, (
        f"over_under missing from recalc build1+build2 ({sorted(shapes)}) — "
        f"F1 floor not reached through the recalc path (F6)"
    )


def test_recalc_shape_overlaps_first_generation(recalc_result):
    """The recalc's interval-shape set substantially overlaps first
    generation's — same content-aware shape, not a divergent skeleton."""
    goal, gen_weeks, _np, all_weeks, info = recalc_result
    if info.get("action") == "no_change":
        pytest.skip("recalc was a no-op for this CTL — nothing routed")
    gen_shapes = _build_shapes(gen_weeks)
    recalc_shapes = _build_shapes(all_weeks)
    assert gen_shapes, "first generation produced no build interval shapes"
    overlap = gen_shapes & recalc_shapes
    # Both routes are the SAME sampler now → the canonical hard shapes recur.
    assert len(overlap) >= 4, (
        f"recalc shapes {sorted(recalc_shapes)} barely overlap first-gen "
        f"{sorted(gen_shapes)} (overlap {sorted(overlap)}) — recalc not routed "
        f"through the sampler"
    )
