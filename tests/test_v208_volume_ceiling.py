"""v2.1.0 (E1+F5) — load-based weekly volume ceiling.

ROOT (pre-fix): a generated plan's REAL weekly volume was one library workout
per available day, each clamped only to that day's availability. With generous
availability (e.g. 3.5h × 7) the plan saturated to ~24.5h / ~1592 TSS no matter
how little the rider had recently been training. ``peak_weekly_tss`` was capped
by ``hours_per_week × 65`` (the availability sum) and never enforced as a weekly
total — the only authoritative clamp was per-DAY.

FIX (3 coupled parts):
  - ride_storage.recent_mean_weekly_tss(): rider's recent mean weekly TSS from
    the full local archive.
  - generate_phases(): weekly ceiling = min(target×7, recent_weekly_tss ×
    ACWR_CEILING) instead of the availability cap; falls back to the legacy
    hours×65 cap when there's no history.
  - _enforce_weekly_volume_ceiling(): a real per-week enforcement pass that
    shrinks the easiest sessions / rests the lowest-value days until the week's
    summed TSS sits at its ceiling — never touching HIT sessions.

The planner is NON-DETERMINISTIC (set/dict ordering), so these are INVARIANT
tests (bounds + monotonicity), not exact-pick assertions. current_ctl and
recent_weekly_tss are passed explicitly so the tests don't depend on the dev
machine's ICU/local archive.
"""
from datetime import date, timedelta
from unittest.mock import patch

import pytest

import ride_storage as rs
import training_planner as tp


# Generous availability: 3.5h (210 min) EVERY day, no goal-enforced rest days —
# the "availability sum" the old cap used would be 24.5h / ~1592 TSS.
_GENEROUS_HPW = 24.5
_DAY_CAP_H = 3.5
_DAY_CAP_MIN = int(_DAY_CAP_H * 60)  # 210


def _generous_goal(hours_per_week: float = _GENEROUS_HPW, weeks: int = 16) -> tp.Goal:
    return tp.Goal(
        goal_type="ctl",
        target_date=date.today() + timedelta(weeks=weeks),
        plan_weeks=weeks,
        hours_per_week=hours_per_week,
        max_weekday_hours=_DAY_CAP_H,
        max_weekend_hours=_DAY_CAP_H,
        available_days=[0, 1, 2, 3, 4, 5, 6],
        rest_days=[],
    )


def _week_tss(week) -> float:
    return sum(s.tss_estimate or 0 for s in week.sessions if s.session_type != "rest")


def _week_minutes(week) -> int:
    return sum(s.duration_min or 0 for s in week.sessions if s.session_type != "rest")


def _peak_nontaper_tss(weeks) -> float:
    """Max summed TSS over non-taper, non-stepback weeks (the binding peak)."""
    return max(
        _week_tss(w) for w in weeks
        if w.phase != "taper" and not w.is_stepback
    )


# ── 1. load-based ceiling binds (NOT the availability sum) ────────────────────

@pytest.mark.parametrize("seed_salt", [0, 7, 4242])
def test_peak_week_bounded_by_recent_load_not_availability(seed_salt):
    """recent_weekly_tss=400 (≈10h) + generous availability → the peak non-taper
    week sits near the load-based ceiling (≤~1.3×400 ×tolerance), NOT the
    ~24.5h / ~1592-TSS availability saturation."""
    recent = 400.0
    ceiling = recent * tp.ACWR_CEILING  # 520
    # tolerance: the enforcement pass acts at >1.05×ceiling and the per-day clamp
    # absorbs the small remainder, so allow a modest band above the raw ceiling.
    upper = ceiling * 1.10

    _phases, weeks = tp.generate_plan(
        _generous_goal(), seed_salt=seed_salt,
        current_ctl=55.0, recent_weekly_tss=recent,
    )
    peak_tss = _peak_nontaper_tss(weeks)
    peak_hours = max(
        _week_minutes(w) for w in weeks
        if w.phase != "taper" and not w.is_stepback
    ) / 60.0

    assert peak_tss <= upper, (
        f"seed={seed_salt}: peak non-taper week {peak_tss:.0f} TSS exceeds the "
        f"load-based ceiling band {upper:.0f} (recent={recent}, ACWR×={tp.ACWR_CEILING}) "
        f"— volume ceiling not enforced"
    )
    # Sanity: the old availability cap was 24.5h/1592 TSS — we must be FAR below.
    assert peak_tss < 900, (
        f"seed={seed_salt}: peak week {peak_tss:.0f} TSS still near the old "
        f"availability saturation (~1592) — ceiling not load-based"
    )
    assert peak_hours < 16.0, (
        f"seed={seed_salt}: peak week {peak_hours:.1f}h near the 24.5h "
        f"availability sum — ceiling not load-based"
    )


# ── 2. no-history → legacy availability cap (backward-safe) ───────────────────

@pytest.mark.parametrize("seed_salt", [0, 7])
def test_no_history_uses_ctl_load_ceiling_not_availability(seed_salt):
    """B3 (v2.1.0): with NO ride history (recent_weekly_tss=None + empty archive)
    but a known CTL, the weekly ceiling now anchors on CTL×7 (a recent-load
    proxy) instead of the legacy availability sum — so an ICU-only / fresh-install
    rider is no longer over-scheduled toward the ~24.5h/1592-TSS availability cap.
    Pre-B3 this fell back to hours_per_week×65."""
    goal = _generous_goal()
    legacy_avail_cap = goal.hours_per_week * 65  # 1592.5 — must NOT bind anymore

    # Empty the archive so the self-fetch returns None (hermetic, ignores the
    # dev machine's real rides).
    with patch.object(rs, "list_rides", return_value=[]):
        phases, _weeks = tp.generate_plan(
            goal, seed_salt=seed_salt,
            current_ctl=55.0, recent_weekly_tss=None,
        )

    peak_target = max(p.weekly_tss_target for p in phases if p.name != "taper")
    # ceiling = min(target×7, (CTL×7)×ACWR). With CTL=55 → CTL×7=385,
    # ×1.3 = 500.5; whichever binds, the peak must sit at/under that load ceiling.
    ctl_load_ceiling = 55.0 * 7 * tp.ACWR_CEILING  # 500.5
    assert peak_target <= ctl_load_ceiling + 1, (
        f"seed={seed_salt}: no-history peak {peak_target:.0f} exceeds the "
        f"CTL-derived load ceiling {ctl_load_ceiling:.0f} — B3 anchor not applied"
    )
    assert peak_target < legacy_avail_cap * 0.5, (
        f"seed={seed_salt}: no-history peak {peak_target:.0f} is near the old "
        f"availability cap {legacy_avail_cap:.0f} — B3 must keep it load-based"
    )


# ── 3. monotonic: higher recent load → higher ceiling (same availability) ─────

def test_higher_recent_load_yields_higher_ceiling():
    """For the SAME generous availability, a rider with higher recent weekly TSS
    gets a strictly higher peak weekly ceiling than a detrained rider."""
    goal = _generous_goal()

    phases_lo, weeks_lo = tp.generate_plan(
        goal, seed_salt=0, current_ctl=40.0, recent_weekly_tss=250.0,
    )
    phases_hi, weeks_hi = tp.generate_plan(
        goal, seed_salt=0, current_ctl=70.0, recent_weekly_tss=550.0,
    )

    target_lo = max(p.weekly_tss_target for p in phases_lo if p.name != "taper")
    target_hi = max(p.weekly_tss_target for p in phases_hi if p.name != "taper")
    assert target_hi > target_lo, (
        f"fit rider peak target {target_hi:.0f} not > detrained {target_lo:.0f} "
        f"for the same availability — ceiling not load-monotonic"
    )

    # And the realized (enforced) peak volume tracks it too.
    peak_lo = _peak_nontaper_tss(weeks_lo)
    peak_hi = _peak_nontaper_tss(weeks_hi)
    assert peak_hi > peak_lo, (
        f"fit rider realized peak {peak_hi:.0f} TSS not > detrained {peak_lo:.0f} "
        f"— enforced volume not load-monotonic"
    )


# ── 4. per-day availability still caps individual session length ──────────────

@pytest.mark.parametrize("seed_salt", [0, 7, 4242])
def test_per_day_availability_caps_session_length(seed_salt):
    """A 3.5h (210-min) day must never yield a >210-min session — the per-day
    availability clamp is untouched by the new weekly ceiling. Checked under the
    load-based path (where the weekly ceiling is also active)."""
    _phases, weeks = tp.generate_plan(
        _generous_goal(), seed_salt=seed_salt,
        current_ctl=70.0, recent_weekly_tss=550.0,
    )
    for w in weeks:
        for s in w.sessions:
            if s.session_type == "rest":
                continue
            assert (s.duration_min or 0) <= _DAY_CAP_MIN, (
                f"seed={seed_salt} week={w.week_num} {s.day} {s.session_type}: "
                f"{s.duration_min}min exceeds the {_DAY_CAP_MIN}min day cap"
            )


# ── invariant: HIT sessions are never dropped by the ceiling pass ─────────────

def test_ceiling_pass_preserves_at_least_one_hit_in_build_weeks():
    """The volume-ceiling pass must shed EASY volume only — every non-stepback
    build/peak week must still carry ≥1 HIT session even under an aggressive
    ceiling (low recent load + generous availability)."""
    _phases, weeks = tp.generate_plan(
        _generous_goal(), seed_salt=0,
        current_ctl=40.0, recent_weekly_tss=250.0,
    )
    for w in weeks:
        if w.phase not in ("build1", "build2", "peak") or w.is_stepback:
            continue
        assert tp._week_hit_count(w) >= 1, (
            f"week={w.week_num} phase={w.phase}: ceiling pass dropped all HIT "
            f"sessions ({[s.session_type for s in w.sessions if s.session_type != 'rest']})"
        )
