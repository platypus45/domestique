"""v1.0.4 IMPL-PLANNER — taxonomy wiring tests.

These tests pin the Wave 2B planner-side wiring:
  1. `anaerobic` is a peak-phase HIT-slot-eligible class (the previously-orphan
     fix: it was weighted in WORKOUT_MIX_PREFERENCE but excluded from
     `_HIT_SLOT_CONTENT_CLASSES`, so 311 anaerobic files were never sampled).
  2. The 6 new structural-variant classes — `endurance_intervals`,
     `tempo_intervals`, `tempo_ladder`, `sweet_spot_ladder`, `threshold_ladder`,
     `vo2_ladder` — are wired through `_CONTENT_TO_PROTOCOL` and appear in
     `WORKOUT_MIX_PREFERENCE` rows for the phases they belong to.
  3. The `mixed` junk-drawer class is dropped from all 6 maps (217 files
     re-routed by IMPL-CLASSIFIER's zone-dominance pass).
  4. With WORKOUT_MIX_PREFERENCE peak-row weights and the slot-eligibility
     filter, `anaerobic` is reachable: across N=10 random seeds, weighted
     sampling from the peak HIT-slot pool selects anaerobic at least once.

These are MAP-LEVEL assertions only — no live `.content_classification.json`
exercised here (Wave 3 QA validates end-to-end picker behavior).
"""
import random

import pytest

import training_planner as tp


_NEW_CLASSES = (
    "endurance_intervals",
    "tempo_intervals",
    "tempo_ladder",
    "sweet_spot_ladder",
    "threshold_ladder",
    "vo2_ladder",
)


# ── Test 1 — anaerobic orphan fix ────────────────────────────────────────────

class TestAnaerobicOrphanFix:
    """Wave 2B fix: `anaerobic` was weighted 5–15% in build/peak rows of
    WORKOUT_MIX_PREFERENCE but missing from `_HIT_SLOT_CONTENT_CLASSES`, so
    the slot eligibility filter rejected every weighted pick. 311 anaerobic
    files in the library were never picked. Adding `anaerobic` to the HIT
    slot set unblocks them."""

    def test_anaerobic_in_hit_slot_eligibility_set(self):
        assert "anaerobic" in tp._HIT_SLOT_CONTENT_CLASSES, (
            "anaerobic must be HIT-slot-eligible — it is weighted in peak/build "
            "rows of WORKOUT_MIX_PREFERENCE but was previously filtered out by "
            "_HIT_SLOT_CONTENT_CLASSES"
        )

    def test_anaerobic_passes_peak_phase_hit_slot_filter(self):
        """The slot pre-filter at training_planner.py:~3206 builds:

            hit_pref = {cc: w for cc, w in row.items()
                        if cc in _HIT_SLOT_CONTENT_CLASSES and w > 0}

        For the peak row, after this filter, `anaerobic` MUST be present with
        a non-zero weight — otherwise the orphan still exists.
        """
        peak_row = tp.WORKOUT_MIX_PREFERENCE["peak"][0]
        hit_pref = {
            cc: w for cc, w in peak_row.items()
            if cc in tp._HIT_SLOT_CONTENT_CLASSES and w > 0
        }
        assert "anaerobic" in hit_pref, (
            f"peak HIT-slot post-filter dict missing anaerobic — orphan still "
            f"present. Filtered keys: {sorted(hit_pref)}"
        )
        assert hit_pref["anaerobic"] > 0


# ── Test 2 — new classes wired through ───────────────────────────────────────

class TestNewClassesWired:
    """The 6 new structural-variant classes are wired through the planner."""

    @pytest.mark.parametrize("cc", _NEW_CLASSES)
    def test_new_class_in_content_to_protocol(self, cc):
        assert cc in tp._CONTENT_TO_PROTOCOL, (
            f"{cc} must have a human-readable protocol label in "
            f"_CONTENT_TO_PROTOCOL"
        )
        assert tp._CONTENT_TO_PROTOCOL[cc], (
            f"{cc} maps to an empty protocol label — fix _CONTENT_TO_PROTOCOL"
        )

    @pytest.mark.parametrize("cc", _NEW_CLASSES)
    def test_new_class_appears_in_some_workout_mix_row(self, cc):
        """Each new class must appear in at least one WORKOUT_MIX_PREFERENCE
        row with non-zero weight — otherwise the planner can never weight it
        in any phase."""
        seen = False
        for phase_name, rows in tp.WORKOUT_MIX_PREFERENCE.items():
            for row in rows:
                if row.get(cc, 0) > 0:
                    seen = True
                    break
            if seen:
                break
        assert seen, (
            f"{cc} has zero weight across every phase/week of "
            f"WORKOUT_MIX_PREFERENCE — the planner will never pick it"
        )

    def test_new_classes_are_interval_shaped(self):
        """*_intervals + *_ladder classes are interval-shaped — the dose
        isn't bunched into a single steady block."""
        for cc in _NEW_CLASSES:
            assert cc in tp._INTERVAL_SHAPED_CONTENT_CLASSES, (
                f"{cc} should be in _INTERVAL_SHAPED_CONTENT_CLASSES — its "
                f"dose is not bunched"
            )

    def test_endurance_intervals_in_endurance_slot(self):
        """`endurance_intervals` (Z2 + strides) belongs in the endurance slot
        pool — it's an aerobic finish-fast variant, not HIT."""
        assert "endurance_intervals" in tp._ENDURANCE_SLOT_CONTENT_CLASSES

    @pytest.mark.parametrize("cc", (
        "tempo_intervals", "tempo_ladder", "sweet_spot_ladder",
        "threshold_ladder", "vo2_ladder",
    ))
    def test_hit_intervals_and_ladders_in_hit_slot(self, cc):
        """tempo_intervals + the 4 ladder classes are interval-shaped HIT —
        eligible on HIT slots, not endurance slots."""
        assert cc in tp._HIT_SLOT_CONTENT_CLASSES, (
            f"{cc} must be HIT-slot-eligible"
        )

    @pytest.mark.parametrize("cc", _NEW_CLASSES)
    def test_new_class_has_min_distinct_target(self, cc):
        """Each new class needs a soft minimum in _PLAN_CLASS_MIN_DISTINCT_24W
        so the sampler's distinct-file bookkeeping covers it."""
        assert cc in tp._PLAN_CLASS_MIN_DISTINCT_24W, (
            f"{cc} missing from _PLAN_CLASS_MIN_DISTINCT_24W"
        )
        # Small classes — 1-3 distinct files for a 24-week plan is the
        # documented range. Keep the upper bound loose to avoid false fails
        # if a future tweak nudges these.
        assert 1 <= tp._PLAN_CLASS_MIN_DISTINCT_24W[cc] <= 5


# ── Test 3 — `mixed` is dropped ──────────────────────────────────────────────

class TestMixedDropped:
    """The `mixed` junk-drawer class is removed from all 6 planner maps.
    The 217 files previously labeled `mixed` are re-routed by IMPL-CLASSIFIER
    via zone-dominance and end up in tempo / threshold / endurance / etc."""

    def test_mixed_not_in_content_to_protocol(self):
        assert "mixed" not in tp._CONTENT_TO_PROTOCOL

    def test_mixed_not_in_hit_slot_classes(self):
        assert "mixed" not in tp._HIT_SLOT_CONTENT_CLASSES

    def test_mixed_not_in_endurance_slot_classes(self):
        assert "mixed" not in tp._ENDURANCE_SLOT_CONTENT_CLASSES

    def test_mixed_not_in_interval_shaped_classes(self):
        assert "mixed" not in tp._INTERVAL_SHAPED_CONTENT_CLASSES

    def test_mixed_not_in_min_distinct_24w(self):
        assert "mixed" not in tp._PLAN_CLASS_MIN_DISTINCT_24W

    def test_mixed_not_in_any_workout_mix_row(self):
        offenders = []
        for phase_name, rows in tp.WORKOUT_MIX_PREFERENCE.items():
            for i, row in enumerate(rows):
                if "mixed" in row:
                    offenders.append((phase_name, i))
        assert not offenders, (
            f"`mixed` still present in WORKOUT_MIX_PREFERENCE rows: {offenders}"
        )


# ── Test 4 — anaerobic IS reachable in peak via weighted sampling ────────────

class TestAnaerobicReachableInPeak:
    """v1.0.4 acceptance gate #5 (map-level reproduction):

    The planner's HIT-slot pre-filter (training_planner.py:~3206) computes:

        hit_pref = {cc: w for cc, w in row.items()
                    if cc in _HIT_SLOT_CONTENT_CLASSES and w > 0}

    ...then samples a class via weighted random choice. With anaerobic now in
    `_HIT_SLOT_CONTENT_CLASSES` AND weighted >0 in the peak row of
    `WORKOUT_MIX_PREFERENCE`, weighted sampling MUST select anaerobic at
    least once across N=10 random seeds. (Pre-fix it would never select
    because the filter rejected it.)
    """

    def _peak_hit_pref(self):
        peak_row = tp.WORKOUT_MIX_PREFERENCE["peak"][0]
        return {
            cc: w for cc, w in peak_row.items()
            if cc in tp._HIT_SLOT_CONTENT_CLASSES and w > 0
        }

    def test_anaerobic_picked_at_least_once_across_10_seeds(self):
        hit_pref = self._peak_hit_pref()
        # Repeat-per-seed mimics multiple HIT slots per peak week (~3) over a
        # plan with ~3 peak weeks — order of 9 picks per seed is realistic.
        picks_per_seed = 9
        keys = list(hit_pref.keys())
        weights = [hit_pref[k] for k in keys]
        anaerobic_appearances = 0
        for seed in range(10):
            rng = random.Random(seed)
            for _ in range(picks_per_seed):
                pick = rng.choices(keys, weights=weights, k=1)[0]
                if pick == "anaerobic":
                    anaerobic_appearances += 1
                    break  # one is enough per seed
        assert anaerobic_appearances >= 1, (
            f"anaerobic was never sampled across 10 random seeds × "
            f"{picks_per_seed} peak-HIT picks each — orphan fix not effective. "
            f"hit_pref={hit_pref}"
        )
