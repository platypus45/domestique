"""v3.2.2 (#14) — pool-bucketing completeness.

Root cause being pinned: the v4.5.0 pool-bucketing sets
(`_HIT_CONTENT_CLASSES` / `_ENDURANCE_CONTENT_CLASSES`) were never updated
for the v1.0.4 16-class taxonomy, so 5 slot-ELIGIBLE classes (ladders +
tempo_intervals; 431 rows) plus endurance_intervals (156 rows) bucketed into
NO pool — their WORKOUT_MIX_PREFERENCE weight was dead and the hard-pick
share silently redistributed by raw file count. This suite guards the
taxonomy from rotting out of the bucketing sets again.

Facts-gate cost note (grill P5): admissibility reads the cached
.workout_facts.json — a full-library sweep is ~10-25 ms, safe for the
parallel gate.
"""
import pytest

import training_planner as tp


@pytest.fixture(scope="module")
def pools():
    lib = tp.load_workout_library()
    return tp._build_pool_indexes(lib)


def test_every_slot_eligible_class_with_files_is_bucketed(pools):
    """Every content class the sampler may PLACE on a slot
    (_HIT_SLOT_CONTENT_CLASSES ∪ _ENDURANCE_SLOT_CONTENT_CLASSES) that has
    ≥1 row surviving the pool gates must appear in ≥1 sampled-from pool —
    otherwise its mix-row weight is dead and the class is unreachable.

    double_threshold is exempt: a protocol pairing (AM+PM), not a file
    class — 0 classified files by design."""
    eligible = (tp._HIT_SLOT_CONTENT_CLASSES | tp._ENDURANCE_SLOT_CONTENT_CLASSES) - {
        "double_threshold",
    }
    pooled_classes = {
        tp._content_class_for_row(w)
        for w in pools["hit"] + pools["endurance"]
    }
    # by_class holds every row that passed admissibility (pre-floor), so a
    # class present there with floor-passing rows but absent from both pools
    # is exactly the bucketing gap this test exists to catch.
    missing = []
    for cc in sorted(eligible):
        rows = pools["by_class"].get(cc, [])
        has_floor_passing = any(
            (w.get("Score", 0) or 0) >= tp._class_aware_score_floor(cc)
            for w in rows
        )
        if has_floor_passing and cc not in pooled_classes:
            missing.append(f"{cc} ({len(rows)} rows)")
    assert not missing, (
        "Slot-eligible classes with admissible files are bucketed into NO "
        f"pool (dead mix weight): {missing}"
    )


def test_bucketing_sets_cover_slot_sets(pools):
    """Set-level guard (no library needed to trip it): every slot-eligible
    class is a member of a bucketing set, so newly-taxonomized classes must
    be wired into pools the moment they become slot-eligible."""
    bucketed = tp._HIT_CONTENT_CLASSES | tp._ENDURANCE_CONTENT_CLASSES
    eligible = (tp._HIT_SLOT_CONTENT_CLASSES | tp._ENDURANCE_SLOT_CONTENT_CLASSES) - {
        "double_threshold",
    }
    missing = sorted(eligible - bucketed)
    assert not missing, f"slot-eligible classes not in any bucketing set: {missing}"


def test_ftp_test_class_rows_never_pooled(pools):
    """v3.2.2 (grill amendment 5): rows CLASSIFIED ftp_test but missing the
    explicit tag used to slip past the tag skip into the normal pools — a
    test protocol must never land on a normal slot."""
    for pool_name in ("hit", "endurance", "all_pool"):
        offenders = [
            w.get("File") for w in pools[pool_name]
            if tp._content_class_for_row(w) == "ftp_test"
        ]
        assert not offenders, f"ftp_test rows in {pool_name}: {offenders[:5]}"
