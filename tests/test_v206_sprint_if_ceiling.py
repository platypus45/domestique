"""v2.0.6 — sprint/neuromuscular slots reject threshold/anaerobic-LOAD workouts.

The content classifier tags by structure (short max-effort segments, high peak
watts) and ignores aggregate load, so ~29% of the files it calls 'neuromuscular'
are really threshold/anaerobic by IF — short recovery (16–30s) keeps average
power near threshold (IF 0.86–1.04). Those landed in sprint slots and rendered
as ~140-TSS "neuromuscular" days. match_zwo now drops candidates above
_SPRINT_SLOT_IF_CEILING from sprint slots only.
"""
from __future__ import annotations

from datetime import date

import training_planner as tp


def _row(lib, fname):
    return next((w for w in lib if w.get("File") == fname), None)


def test_sprint_slot_never_exceeds_if_ceiling():
    lib = tp.load_workout_library()
    ceiling = tp._SPRINT_SLOT_IF_CEILING
    for dur in (45, 60, 90):
        for seed in range(8):
            s = tp.PlannedSession(
                day=date(2026, 6, 16), day_name="Tue", session_type="sprint",
                duration_min=dur, tss_estimate=140, description="")
            tp.match_zwo(s, lib, week_num=seed, day_idx=1, seed_salt=seed)
            row = _row(lib, s.zwo_file)
            assert row is not None, f"no row for pick {s.zwo_file!r} (dur={dur} seed={seed})"
            assert float(row.get("IF") or 0) <= ceiling, (
                f"sprint slot picked IF {row.get('IF')} > {ceiling} "
                f"({s.zwo_file}, dur={dur}, seed={seed})")


def test_over_cooked_neuromuscular_file_unreachable_from_sprint():
    """The specific mislabeled file that triggered this (IF 0.87) must never be
    chosen for a sprint slot, at any duration/seed."""
    # Re-pinned after the W′-feasibility wave (1de456dd): the live fixture
    # (IF 0.87) was rest-lengthened to W′ feasibility and its IF fell below
    # the ceiling, so the live-file precondition can no longer hold. NOTE:
    # W′ feasibility and this IF ceiling are ORTHOGONAL screens — plenty of
    # feasible neuromuscular files still average IF>0.82 (the ceiling gate
    # exists exactly for them). So: resurrect the fixture's old over-cooked
    # value synthetically (canary pattern) and prove the ceiling still
    # rejects it end-to-end through match_zwo.
    lib = tp.load_workout_library()
    base = _row(lib, "neuromuscular_26x10s_200pct_108min.zwo")
    if base is None:
        return  # library variant without this file — nothing to assert
    resurrected = dict(base, IF=0.87)  # pre-amendment over-cooked value
    doctored = [resurrected if w is base else w for w in lib]
    picks = set()
    for dur in (90, 110):
        for seed in range(16):
            s = tp.PlannedSession(
                day=date(2026, 6, 16), day_name="Tue", session_type="sprint",
                duration_min=dur, tss_estimate=140, description="")
            tp.match_zwo(s, doctored, week_num=seed, day_idx=1, seed_salt=seed)
            picks.add(s.zwo_file)
    assert "neuromuscular_26x10s_200pct_108min.zwo" not in picks


def test_ceiling_does_not_apply_to_non_sprint_slots():
    """The gate is sprint-only — high-IF files must still be reachable for the
    slots they belong to (vo2max/threshold), or we'd starve those pools."""
    lib = tp.load_workout_library()
    hi = [w for w in lib if float(w.get("IF") or 0) > tp._SPRINT_SLOT_IF_CEILING
          and tp._content_class_for_row(w) in ("vo2max", "vo2_short", "threshold")]
    assert hi, "fixture expectation: library has high-IF vo2/threshold files"
    seen_hi = False
    for seed in range(12):
        s = tp.PlannedSession(
            day=date(2026, 6, 16), day_name="Tue", session_type="vo2max",
            duration_min=75, tss_estimate=90, description="")
        tp.match_zwo(s, lib, week_num=seed, day_idx=1, seed_salt=seed)
        row = _row(lib, s.zwo_file)
        if row and float(row.get("IF") or 0) > tp._SPRINT_SLOT_IF_CEILING:
            seen_hi = True
            break
    assert seen_hi, "vo2max slot should still be able to pick an IF>0.82 workout"
