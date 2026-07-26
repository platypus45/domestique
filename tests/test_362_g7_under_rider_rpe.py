"""v3.6.0 — characterise G7 now that the rider's own RPE actually reaches it.

G7 (3-day mean session-RPE ≥ 7 + a hard session planned → drop one intensity
step, Foster 1998) already existed, but it only ever read ICU's imported
`feel` / `perceived_exertion`. The rating the rider gives in this app is
written under `rpe`, so before v3.6.0 it reached nothing — the "feed RPE back
into the planner" loop was open at one line in `_last_3d_mean_feel`.

These tests pin what that now does, deliberately, rather than leaving it
emergent. Two properties matter and are asserted below:

  * The threshold stays Foster's published ≥7. A single rating carries real
    noise (sRPE test-retest CV 28.1%, Wallace 2014 PMID 24662229), so nothing
    here reacts to a 1-unit move; the only effect of a high rating is LESS
    load, never more. Asymmetric and fail-safe on purpose.
  * A rating never inflates through a scale mix-up. `feel` is a 1-5
    satisfaction rating; a real CR-10 number always wins outright.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

import training_planner as tp

_TODAY = date.today().isoformat()
_YDAY = (date.today() - timedelta(days=1)).isoformat()


def _planned(session_type="vo2max"):
    return tp.PlannedSession(
        day=None, day_name="Tue", session_type=session_type,
        duration_min=75, tss_estimate=95, description="4x4",
    )


def _neutral_readiness():
    """Readiness that fires no other gate, so G7 is what we are measuring."""
    return {"score": 75, "status": "GOOD",
            "dfa_cap": {"cap_applied": False},
            "decoupling_advisory": {"advisory": False}}


def _adjust(rides, session_type="vo2max"):
    return tp.adjust_today_session(
        _planned(session_type), _neutral_readiness(), rides_recent=rides)


# ── the loop is closed ──────────────────────────────────────────────────────

def test_a_rating_given_in_this_app_reaches_the_gate():
    adjusted, reason = _adjust([{"date": _TODAY, "rpe": 8}])
    assert adjusted.session_type != "vo2max"
    assert "RPE" in reason


def test_the_gate_reads_the_same_value_the_rider_entered():
    assert tp._last_3d_mean_feel([{"date": _TODAY, "rpe": 8}]) == 8.0


def test_zero_is_a_rating_not_a_missing_value():
    """Foster CR-10 starts at 0 ("nothing at all"), so 0 must not read as
    absent — that would silently drop the lowest rating from the mean."""
    assert tp._last_3d_mean_feel([{"date": _TODAY, "rpe": 0}]) == 0.0


def test_local_rating_wins_over_every_imported_field():
    v = tp._last_3d_mean_feel([{"date": _TODAY, "rpe": 3,
                                "perceived_exertion": 9, "feel": 5}])
    assert v == 3.0, "the rider's own rating must not be averaged away"


def test_rating_inside_raw_json_is_still_found():
    v = tp._last_3d_mean_feel([{"date": _TODAY, "raw_json": {"rpe": 9}}])
    assert v == 9.0


# ── the threshold, and what it deliberately does NOT do ─────────────────────

@pytest.mark.parametrize("rpe,downgrades", [
    (10, True), (8, True), (7, True),     # ≥7 — Foster's published cut
    (6, False), (4, False), (0, False),   # below it, nothing happens
])
def test_published_threshold_is_unchanged(rpe, downgrades):
    adjusted, _ = _adjust([{"date": _TODAY, "rpe": rpe}])
    assert (adjusted.session_type != "vo2max") is downgrades


def test_effect_is_one_step_down_not_a_collapse_to_rest():
    adjusted, _ = _adjust([{"date": _TODAY, "rpe": 9}])
    assert adjusted.session_type not in ("rest", "recovery")
    assert adjusted.tss_estimate < 95


def test_a_high_rating_can_only_reduce_load_never_add_it():
    """The asymmetry is intentional: RPE is noisy enough (CV 28%) that it may
    talk us out of intensity, never into it."""
    easy, _ = _adjust([{"date": _TODAY, "rpe": 10}], session_type="z2")
    assert easy.session_type == "z2"
    assert easy.tss_estimate == 95


def test_mean_over_the_window_dilutes_one_outlier():
    """Two easy days plus one 10 averages under the cut — a single spike does
    not by itself rewrite the week."""
    rides = [{"date": _TODAY, "rpe": 10},
             {"date": _YDAY, "rpe": 4},
             {"date": _YDAY, "rpe": 4}]
    assert tp._last_3d_mean_feel(rides) == 6.0
    adjusted, _ = _adjust(rides)
    assert adjusted.session_type == "vo2max"


def test_ratings_outside_the_window_are_ignored():
    old = (date.today() - timedelta(days=9)).isoformat()
    assert tp._last_3d_mean_feel([{"date": old, "rpe": 10}]) is None


def test_no_rating_anywhere_leaves_the_session_alone():
    adjusted, _ = _adjust([{"date": _TODAY}])
    assert adjusted.session_type == "vo2max"


# ── the defect this characterisation exposed ────────────────────────────────

def test_stepping_down_the_ladder_never_raises_the_load_estimate():
    """`TSS_PER_HOUR` is ordered by sustainable hourly load, the intensity
    ladder by intensity. threshold is 90/h against vo2max's 75/h, so at an
    unchanged duration a vo2max→threshold "de-escalation" ADDED ~20% load —
    every protective gate could hand the rider more work than it removed.
    The rule had been fixed once, on the one path that got reported (the
    manual tier-down); every sibling caller was still wrong."""
    for old_type in sorted(tp._HARD_SESSION_TYPES):
        new_type = tp._drop_intensity(old_type)
        if new_type == old_type:
            continue
        old_tss = round(75 / 60 * tp.TSS_PER_HOUR.get(old_type, 75))
        dur, tss = tp._deescalated_load(75, new_type, old_tss)
        assert tss <= old_tss, f"{old_type}->{new_type}: {old_tss} -> {tss}"
        assert dur >= tp._VOLUME_MIN_SESSION_MIN


def test_the_load_is_held_by_trimming_time_not_by_lying_about_it():
    """Clamping the number while keeping the duration would leave an
    inconsistent record (75 min of threshold labelled as 95 TSS). The
    duration moves so type × duration × TSS still agree."""
    dur, tss = tp._deescalated_load(75, "threshold", 95)
    assert tss == round(dur / 60 * tp.TSS_PER_HOUR["threshold"])
    assert dur < 75


def test_a_genuinely_easier_type_keeps_the_full_duration():
    dur, tss = tp._deescalated_load(75, "z2", 95)
    assert dur == 75 and tss < 95


def test_no_prior_estimate_falls_back_to_the_table():
    dur, tss = tp._deescalated_load(60, "tempo", None)
    assert (dur, tss) == (60, tp.TSS_PER_HOUR["tempo"])


def test_g7_downgrade_reduces_load_end_to_end():
    adjusted, _ = _adjust([{"date": _TODAY, "rpe": 9}])
    assert adjusted.tss_estimate <= 95
