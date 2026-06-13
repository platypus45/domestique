"""Domestique v2.0.0 — event-demand + goal-focus planner acceptance tests.

Covers the v1.11.0/v2.0.0 IMPL-EVENT + IMPL-GOAL-FOCUS surface in
``training_planner.py``:

  * ``_event_demand_targets``       — event goal → plan-ready demand dict, or
                                      None for every non-event / missing-athlete
                                      / absurd / past-event input.
  * ``target_ctl_for_event``        — band × (0.94 + 0.12·difficulty); legacy
                                      discrete steps when difficulty is None.
  * ``generate_plan`` (event)       — event SIZE drives the weekend long ride
                                      toward the endurance target, honestly
                                      capped by ``max_weekend_hours``.
  * ``regenerate_from_today``       — same demand-driven long ride survives a
                                      regen (no collapse back to a generic ~2h).
  * ``GOAL_CLASS_EMPHASIS`` /       — ftp/vo2max goals tilt the HIT-class mix
    ``_apply_goal_emphasis``          toward their target family in build2.

Style mirrors ``tests/test_planner_full_library_utilization.py`` (``_build_*``
helpers, ``_picked_files``) and ``tests/test_availability_reforecast.py``
(``_next_monday`` future-dating). All sessions are future-dated off the next
Monday so the demand model + long-ride ramp aren't truncated by "today".

ROBUSTNESS: every assertion uses ``>=`` / ranges / best-or-sum-over-seeds, NOT
exact equality — the sampler is RNG-driven and the per-week long-ride ramp
peaks on a phase-dependent week. The sole exception is the non-focus invariant
(#8), which MUST be byte-identical because the athlete is ignored for non-event
goals. ``pytest.skip`` guards classes the local library can't populate.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

import pytest

import training_planner as tp


# ── shared fixtures / helpers ────────────────────────────────────────────────

ATHLETE = {"ftp": 250, "weight_kg": 72}


def _next_monday() -> date:
    """Next strictly-future Monday so every planned session is after today."""
    today = date.today()
    days = (7 - today.weekday()) % 7
    return today + timedelta(days=days or 7)


def _event_goal(*, km: float, climb: float, weeks: int = 20,
                max_weekend_hours: float = 5.0,
                event_type: str = "granfondo") -> tp.Goal:
    """A future-dated event Goal with a fully-available 7-day week."""
    target = _next_monday() + timedelta(weeks=weeks)
    return tp.Goal(
        goal_type="event",
        event_type=event_type,
        event_km=km,
        event_climb_m=climb,
        target_date=target,
        hours_per_week=10.0,
        max_weekday_hours=2.0,
        max_weekend_hours=max_weekend_hours,
        available_days=[0, 1, 2, 3, 4, 5, 6],
        rest_days=[0],
        daily_max_hours={},
        plan_weeks=weeks,
    )


def _simple_goal(goal_type: str, *, weeks: int = 16) -> tp.Goal:
    """A non-event Goal (general/ftp/vo2max) with the same calendar shape."""
    target = _next_monday() + timedelta(weeks=weeks)
    return tp.Goal(
        goal_type=goal_type,
        target_date=target,
        hours_per_week=10.0,
        max_weekday_hours=2.0,
        max_weekend_hours=4.0,
        available_days=[0, 1, 2, 3, 4, 5, 6],
        rest_days=[0],
        daily_max_hours={},
        plan_weeks=weeks,
    )


def _lr(weeks: list) -> int:
    """Max duration (min) of any endurance (z2 / long_z2) session in a plan."""
    durs = [
        (s.duration_min or 0)
        for w in weeks for s in w.sessions
        if getattr(s, "session_type", "") in ("z2", "long_z2")
    ]
    return max(durs) if durs else 0


def _best_lr(goal: tp.Goal, *, athlete: dict | None, salts: int = 4) -> int:
    """Best (max) long ride across a few seeds — the ramp peaks on a
    phase-dependent week, so a single seed under-reports the reachable max."""
    best = 0
    for s in range(salts):
        _, weeks = tp.generate_plan(goal, athlete=athlete, seed_salt=s)
        best = max(best, _lr(weeks))
    return best


# ── 1–4: _event_demand_targets gating + happy path ───────────────────────────

def test_event_demand_targets_none_for_non_event():
    """Non-event goals never get a demand dict (the v1.11.0 invariant — every
    event-wiring site must no-op so non-event plans stay byte-identical)."""
    fs = {"current_ctl": 50.0}
    for gt in ("general", "ftp", "vo2max"):
        g = _simple_goal(gt)
        assert tp._event_demand_targets(g, ATHLETE, fs) is None, (
            f"goal_type={gt!r} should yield None, not a demand dict"
        )


def test_event_demand_targets_none_for_missing_athlete():
    """An event goal with no usable athlete (None or {} — no ftp/weight) can't
    drive the demand model → None, so the plan falls back to the legacy band."""
    g = _event_goal(km=200, climb=3500)
    fs = {"current_ctl": 50.0}
    assert tp._event_demand_targets(g, None, fs) is None
    assert tp._event_demand_targets(g, {}, fs) is None
    # An athlete missing only weight (or only ftp) is equally unusable.
    assert tp._event_demand_targets(g, {"ftp": 250}, fs) is None
    assert tp._event_demand_targets(g, {"weight_kg": 72}, fs) is None


def test_event_demand_targets_none_for_event_in_past():
    """A target_date in the past → None (can't plan toward a finished event)."""
    g = _event_goal(km=200, climb=3500)
    g.target_date = date.today() - timedelta(days=5)
    assert tp._event_demand_targets(g, ATHLETE, {"current_ctl": 50.0}) is None


def test_event_demand_targets_returns_targets():
    """A valid 200km/3500m granfondo (ftp250/weight72) yields a demand dict
    with all the documented keys, a real predicted finish, a positive long-ride
    target, and difficulty normalised into [0, 1]."""
    g = _event_goal(km=200, climb=3500)
    t = tp._event_demand_targets(g, ATHLETE, {"current_ctl": 50.0})
    assert t is not None
    for key in ("difficulty", "long_target_h", "long_start_h", "climbing_bias",
                "predicted_finish_h", "predicted_tss", "gap_endurance_h"):
        assert key in t, f"missing key {key!r} in demand dict {t!r}"
    assert t["predicted_finish_h"] > 0
    assert t["long_target_h"] > 0
    assert 0.0 <= t["difficulty"] <= 1.0
    # A 200km/3500m event is steep (>12 m/km) → climbing specificity flagged.
    assert t["climbing_bias"] is True


# ── 5–6: event size drives the long ride, honestly capped ────────────────────

def test_event_size_drives_long_ride():
    """A big event (200km/3500m) produces a larger max long ride than a small
    one (60km/300m) at the SAME weekend-hours / dates. The big plan reaches a
    genuine multi-hour ride (>= 240 min); the small one stays shorter."""
    big = _event_goal(km=200, climb=3500, max_weekend_hours=5.0)
    small = _event_goal(km=60, climb=300, max_weekend_hours=5.0)
    big_lr = _best_lr(big, athlete=ATHLETE)
    small_lr = _best_lr(small, athlete=ATHLETE)
    assert big_lr >= small_lr, (
        f"big event long ride {big_lr} < small event long ride {small_lr}"
    )
    assert big_lr >= 240, (
        f"big event long ride only reached {big_lr} min; expected a multi-hour "
        f"(>= 240) ride with max_weekend_hours=5.0"
    )


def test_long_ride_capped_by_weekend_hours():
    """The long ride is an HONEST availability cap: the same big event with
    max_weekend_hours=3.0 must NOT exceed ~3h (180 min, +a little tolerance for
    rounding / the long-Z2 reclassification), even though the event 'wants'
    more endurance volume."""
    big = _event_goal(km=200, climb=3500, max_weekend_hours=3.0)
    lr = _best_lr(big, athlete=ATHLETE)
    assert lr > 0, "expected at least one endurance session in the plan"
    assert lr <= 180 + 10, (
        f"long ride {lr} min exceeds the 3h weekend cap (180 + tolerance) — "
        f"availability is not being honoured"
    )


# ── 7: generate vs regenerate — no drift ─────────────────────────────────────

def test_generate_vs_regen_no_drift():
    """The demand-driven long ride must survive a regen — regenerating the
    SAME event plan from today should keep a large long ride, not collapse it
    back to a generic ~2h (120 min) endurance ride."""
    big = _event_goal(km=200, climb=3500, max_weekend_hours=5.0)
    gen_lr = _best_lr(big, athlete=ATHLETE)
    assert gen_lr >= 240, (
        f"sanity: generate_plan long ride {gen_lr} should already be large"
    )
    # Regenerate from today off a freshly generated plan; take the best long
    # ride across seeds (same ramp-peak caveat as generate).
    _, base_weeks = tp.generate_plan(big, athlete=ATHLETE, seed_salt=0)
    best_regen = 0
    for s in range(4):
        _, regen_weeks, _info = tp.regenerate_from_today(
            big, base_weeks, 50.0, athlete=ATHLETE, seed_salt=s)
        best_regen = max(best_regen, _lr(regen_weeks))
    assert best_regen >= 240, (
        f"regenerate_from_today long ride collapsed to {best_regen} min "
        f"(< 240); the event demand was lost on regen (generate was {gen_lr})"
    )


# ── 8: non-focus invariant — athlete ignored for non-event goals ─────────────

def test_non_focus_goal_invariant():
    """A ``general`` goal is NOT event/focus-driven, so passing an athlete must
    change nothing: the (session_type, duration_min) signature is byte-identical
    with vs without the athlete. Fixed seed_salt isolates the athlete as the
    only variable."""
    g = _simple_goal("general")
    salt = 7

    def sig(weeks):
        return [(s.session_type, s.duration_min)
                for w in weeks for s in w.sessions]

    _, w_no = tp.generate_plan(g, seed_salt=salt)
    _, w_yes = tp.generate_plan(g, athlete=ATHLETE, seed_salt=salt)
    assert sig(w_no) == sig(w_yes), (
        "general goal session signature differs with vs without athlete — "
        "athlete must be ignored for non-event/non-focus goals"
    )


# ── 9: target CTL difficulty nudge ───────────────────────────────────────────

def test_target_ctl_difficulty_nudge():
    """``target_ctl_for_event`` nudges CTL up with difficulty (band ×
    (0.94 + 0.12·d)): d=1.0 > d=0.0, and both stay within ±8% of the band
    anchor (the long ride, not CTL, is the real event lever — CTL moves only
    ±6%)."""
    g = _event_goal(km=200, climb=3500)
    low = tp.target_ctl_for_event(g, 0.0)
    high = tp.target_ctl_for_event(g, 1.0)
    assert high > low, f"difficulty nudge not monotonic: d1={high} !> d0={low}"
    band = tp.EVENT_CTL_TARGETS[g.event_type]["strong"]
    assert 0.92 * band <= low <= 1.08 * band
    assert 0.92 * band <= high <= 1.08 * band


# ── 10: goal focus shifts the HIT-class mix ──────────────────────────────────

def _build2_phase(goal: tp.Goal):
    """Pull the build2 Phase out of the generated phase tree (None if the plan
    is too short to contain one)."""
    for p in tp.generate_phases(goal, 50.0, None):
        if p.name == "build2":
            return p
    return None


def _session_type_counts(goal_type: str, *, seed_salt: int,
                         library: list, pool_index: dict,
                         weeks: int = 12) -> Counter:
    """Sample ~12 build2 weeks for ``goal_type`` and tally session_type picks.

    Mirrors generate_plan's sampler call (per-phase budget, rolling used_names
    + recent_hit_types) but holds the phase fixed at build2 so the only varying
    input is the goal_type → emphasis tilt.
    """
    g = _simple_goal(goal_type, weeks=20)
    phase = _build2_phase(g)
    if phase is None:
        return Counter()
    budget = tp.get_budget_for_phase("build2")
    counts: Counter = Counter()
    used: dict[str, int] = {}
    rot: list[str] = []
    week_start0 = _next_monday()
    for wk in range(1, weeks + 1):
        sessions = tp.sample_week_workouts(
            phase=phase, budget=budget, library=library,
            used_names=used, week_num=wk, seed_salt=seed_salt,
            week_start=week_start0 + timedelta(weeks=wk),
            available_days=g.available_days, rest_days=g.rest_days,
            daily_max_hours=g.daily_max_hours,
            max_weekday_hours=g.max_weekday_hours,
            max_weekend_hours=g.max_weekend_hours,
            pool_index=pool_index, recent_hit_types=rot,
            goal_type=goal_type,
        )
        for s in sessions:
            st = getattr(s, "session_type", "") if s else ""
            if st:
                counts[st] += 1
    return counts


def test_goal_focus_shifts_mix():
    """Over a build2 phase, an ``ftp`` goal yields >= as many 'threshold' picks
    as ``general``, and a ``vo2max`` goal yields MORE 'vo2max' picks than
    ``general``. Summed over 5 seeds so RNG variety (which adds ±1-2/seed
    noise) can't flip the inequality. Soft (>=) on the ftp axis per the spec —
    the FTP emphasis lifts threshold without zeroing other families."""
    library = tp.load_workout_library()
    pool_index = tp._build_pool_indexes(library)

    # Skip gracefully if the local library can't furnish threshold/vo2max HIT
    # work at all (a single general build2 should surface both — if not, the
    # mix-shift assertion is meaningless).
    probe = _session_type_counts("general", seed_salt=0,
                                  library=library, pool_index=pool_index)
    if probe.get("threshold", 0) == 0 or probe.get("vo2max", 0) == 0:
        pytest.skip(
            "library too narrow for goal-focus mix test "
            f"(general build2 threshold={probe.get('threshold', 0)}, "
            f"vo2max={probe.get('vo2max', 0)})"
        )

    gen = Counter()
    ftp = Counter()
    vo2 = Counter()
    for salt in range(5):
        gen += _session_type_counts("general", seed_salt=salt,
                                    library=library, pool_index=pool_index)
        ftp += _session_type_counts("ftp", seed_salt=salt,
                                    library=library, pool_index=pool_index)
        vo2 += _session_type_counts("vo2max", seed_salt=salt,
                                    library=library, pool_index=pool_index)

    assert ftp["threshold"] >= gen["threshold"], (
        f"ftp goal threshold picks {ftp['threshold']} < general "
        f"{gen['threshold']} (summed over 5 seeds)"
    )
    assert vo2["vo2max"] > gen["vo2max"], (
        f"vo2max goal vo2max picks {vo2['vo2max']} not > general "
        f"{gen['vo2max']} (summed over 5 seeds)"
    )


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
