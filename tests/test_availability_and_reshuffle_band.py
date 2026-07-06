"""Tester bugs (post-v3.2.2): availability is a hard promise + reshuffle variety.

Bug A: v4.6.0's +25-min feasibility headroom meant a 60-min-availability day
legally drew 85-min files (and the rematch band stretched that past 90).
Now the upper window is max_min + 5 (rounding tolerance only) in BOTH the
main feasibility filter and the emergency all_pool fallback.

Bug B: on a sparse cell the exact-duration band (max(8%, 3min)) can hold a
single alternative — every Reshuffle click re-offered the same file. The
retry loop now widens the band to max(15%, 10min) after 8 exhausted-band
attempts (match_zwo(widen_band=True)).
"""
import datetime

import pytest

import training_planner as tp


# ── Bug A: per-day availability cap ────────────────────────────────────────

@pytest.mark.parametrize("phase_name", ["base", "build1"])
def test_sampler_respects_60min_days(phase_name):
    lib = tp.load_workout_library()
    pool_index = tp._build_pool_indexes(lib)
    budget = tp.BUDGETS[phase_name]
    phase = tp.Phase(name=phase_name, start=datetime.date(2026, 7, 6),
                     end=datetime.date(2026, 7, 27), weeks=3, focus="",
                     weekly_tss_target=500, z2_pct=0.8, hit_per_week=2,
                     session_types=[])
    offenders = []
    for salt in range(8):
        sessions = tp.sample_week_workouts(
            phase, budget, lib, {}, week_num=2, seed_salt=salt,
            week_start=datetime.date(2026, 7, 6),
            available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[0],
            daily_max_hours={i: 1.0 for i in range(7)},  # 60 min EVERY day
            max_weekday_hours=1.0, max_weekend_hours=1.0,
            pool_index=pool_index, week_in_phase=1, recent_hit_types=[])
        for s in sessions:
            if s.session_type == "rest":
                continue
            if (s.duration_min or 0) > 65:  # 60 + 5 tolerance
                offenders.append((salt, s.day_name, s.session_type, s.duration_min))
    assert not offenders, f"sessions exceed 60-min availability: {offenders}"


def test_generate_plan_respects_weekday_hours():
    """End-to-end: 1.0h weekdays must not carry 90-min sessions (the exact
    tester report). Weekends are separate (3.0h). Two seeds, invariant-based
    (planner-test-nondeterminism rule)."""
    goal = tp.Goal(
        goal_type="general", plan_weeks=8,
        hours_per_week=7.0, max_weekday_hours=1.0, max_weekend_hours=3.0,
        available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[4],
    )
    for seed in (3, 11):
        _ph, weeks = tp.generate_plan(
            goal, athlete={"ftp": 250, "weight_kg": 70},
            recent_weekly_tss=350, seed_salt=seed)
        offenders = []
        for w in weeks:
            for s in w.sessions:
                if s is None or s.session_type == "rest":
                    continue
                if s.day.weekday() >= 5:  # weekend — different cap
                    continue
                if getattr(s, "is_race", False):
                    continue
                if (s.duration_min or 0) > 65:
                    offenders.append((seed, str(s.day), s.session_type,
                                      s.duration_min))
        assert not offenders, (
            f"weekday sessions exceed the 60-min availability: {offenders}")


# ── Bug B: widen_band gives a genuinely different reshuffle pick ──────────

def _row(name, dur):
    return {"Name": name, "File": f"{name}.zwo", "Duration(min)": float(dur),
            "TSS": 60.0, "IF": 0.8, "Score": 7, "Protocol": "Threshold",
            "Z1%": 10.0, "Z2%": 30.0, "Z3%": 10.0, "Z4%": 50.0, "Z5%": 0.0,
            "Z6%": 0.0, "Tags": [], "ContentClass": "threshold",
            "ContentConfidence": 1.0, "SecondaryFlags": {}}


def test_widen_band_reaches_beyond_sparse_tier(monkeypatch):
    # Facts gate would fail-closed on synthetic rows — not under test here.
    monkeypatch.setattr(tp, "file_admissible", lambda st, w: True)
    lib = [_row("thr_80", 80), _row("thr_90", 90), _row("thr_100", 100)]

    def picks(widen):
        seen = set()
        for v in range(1, 13):
            s = tp.PlannedSession(
                day=datetime.date(2026, 7, 6), day_name="Mon",
                session_type="threshold", duration_min=90,
                tss_estimate=90.0, description="")
            s.profile_id = str(v)
            tp.match_zwo(s, lib, week_num=v * 100, day_idx=0,
                         used_names=set(), raise_on_empty=True,
                         exact_duration=True, widen_band=widen)
            seen.add(s.zwo_file)
        return seen

    # Normal band max(8%×90, 3) = 7.2 → only the 90' file qualifies.
    assert picks(widen=False) == {"thr_90.zwo"}
    # GA4 (grill P3): the widened band grows DOWNWARD only —
    # [90−max(15%,10), 90+5] = [76.5, 95] → the 80' file becomes reachable,
    # the 100' file stays out (availability holds even on reshuffle).
    assert picks(widen=True) == {"thr_80.zwo", "thr_90.zwo"}
