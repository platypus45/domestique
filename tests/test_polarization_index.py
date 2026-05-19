"""v1.8.0 — polarization-index + PI-band classification helpers.

References:
- Treff G, Winkert K, Sareban M, Steinacker JM, Sperlich B (2019). "The
  Polarization-Index: A Simple Calculation to Distinguish Polarized From
  Non-polarized Training Intensity Distributions." Frontiers in Physiology,
  10:707. doi:10.3389/fphys.2019.00707
- FastFitness.tips classification heuristic (also used by intervals.icu).

Domestique uses the FastFitness.tips formulation
``PI = log10((Z1+Z2 + Z5+) / Z3+Z4)`` — equivalent in spirit to Treff
(emphasises the easy/hard ratio over the moderate band) and matches the
single ``polarization_index`` value ICU reports on the activity GET.

Classification (v1.8.0 PI-BAND CASCADE): Treff 2019 PI-band rules (see
analytics.classify_distribution docstring + MASTER_DECISIONS_v180_addendum
§F3 verification table). Tests below verify the post-v1.8.0 behaviour;
the legacy centroid-distance expectations are replaced.
"""
from __future__ import annotations

import math
import unittest

from analytics import (
    classification_confidence,
    classify_distribution,
    compute_polarization_block,
    polarization_index,
)


class TestPolarizationIndex(unittest.TestCase):

    def test_returns_none_when_z3z4_is_zero(self):
        # Formula divides by Z3+Z4 — return None to avoid log(0) / div-by-zero.
        self.assertIsNone(polarization_index(80.0, 0.0, 20.0))
        self.assertIsNone(polarization_index(80.0, 0.05, 20.0))

    def test_polarized_distribution_has_positive_pi(self):
        # Heavy easy + meaningful Z5+, low Z3+Z4 → strongly positive PI.
        # log10((78+17)/5) = log10(19) ≈ 1.28
        pi = polarization_index(78.0, 5.0, 17.0)
        self.assertIsNotNone(pi)
        self.assertGreater(pi, 1.0)

    def test_pyramidal_distribution_has_small_positive_pi(self):
        # Treff's textbook pyramidal: large easy, moderate, small hard.
        # log10((65+10)/25) = log10(3.0) ≈ 0.48
        pi = polarization_index(65.0, 25.0, 10.0)
        self.assertIsNotNone(pi)
        self.assertGreater(pi, 0.0)
        self.assertLess(pi, 1.0)

    def test_user_may_1_ride_zwolle_47_9_34_2_17_9(self):
        # The Zwolle Fietsen ride at the heart of v4.5.5 IMPL-DETAIL-SERVER:
        # log10((47.9+17.9)/34.2) = log10(1.924) ≈ 0.28.
        pi = polarization_index(47.9, 34.2, 17.9)
        self.assertAlmostEqual(pi, 0.28, places=2)

    def test_classify_pure_base(self):
        # Z1+Z2 ≥ 70 with negligible z3z4 → base.
        self.assertEqual(classify_distribution(92.0, 6.0, 2.0), "base")

    def test_classify_high_z1z2_low_pi_is_base(self):
        # v1.8.0 PI-band: a distribution at the legacy polarized centroid
        # (78/5/17, PI ≈ 1.28) does NOT clear the PI > 2.0 threshold for
        # polarized. With z3z4 < 30 the band cascade falls through to z1z2
        # ≥ 70 → base. Treff's PI > 2.0 cutoff is the contract.
        self.assertEqual(classify_distribution(78.0, 5.0, 17.0), "base")

    def test_classify_high_z1z2_moderate_z3z4_is_base(self):
        # 70/20/10 — z3z4 too small for pyramidal (< 35) or threshold (< 30),
        # z1z2 ≥ 70 → base.
        self.assertEqual(classify_distribution(70.0, 20.0, 10.0), "base")

    def test_classify_hiit_when_z5plus_dominant(self):
        # PI-band hiit rule: z5+ > 40 AND z1z2 < 20.
        # 40/25/35 — z5+ only 35% and z1z2 = 40% → fails hiit gate; also
        # outside threshold/pyramidal bands → unique.
        self.assertEqual(classify_distribution(40.0, 25.0, 35.0), "unique")
        # A genuine hiit distribution clears the gate:
        self.assertEqual(classify_distribution(10.0, 30.0, 60.0), "hiit")

    # --- v1.8.0 PI-band cascade ---

    def test_classify_user_may_1_ride_under_pi_band(self):
        # 47.9/34.2/17.9 — v1.8.0 fell through to "unique". v1.8.3 added
        # a moderate-pyramid branch (z3z4 >= 20 AND z3z4 > z5+ AND
        # 40 <= z1z2 < 70 → pyramidal) — matches ICU's FastFitness.Tips
        # which calls this distribution a textbook pyramid (Z1+Z2 base,
        # smaller Z3+Z4 middle, tiny Z5+ peak).
        self.assertEqual(classify_distribution(47.9, 34.2, 17.9), "pyramidal")

    def test_classify_polarized_requires_pi_above_2(self):
        # Distribution at the legacy polarized centroid (80/5/15) has
        # PI ≈ 1.28 — below the 2.0 polarized cutoff. Falls through to base.
        self.assertEqual(classify_distribution(80.0, 5.0, 15.0), "base")

    def test_classify_at_legacy_pyramidal_centroid_is_base(self):
        # 80/15/5 — z3z4 only 15 (< 30) and z1z2 ≥ 70 → base.
        # The PI-band cascade no longer awards "pyramidal" to centroid
        # proximity alone; pyramidal requires z3z4 ≥ 35.
        self.assertEqual(classify_distribution(80.0, 15.0, 5.0), "base")
        # Confidence remains in [0.5, 1.0] for any in-band label.
        self.assertGreaterEqual(classification_confidence(80.0, 15.0, 5.0), 0.5)
        self.assertLessEqual(classification_confidence(80.0, 15.0, 5.0), 1.0)

    def test_classify_threshold_under_pi_band(self):
        # 30/60/10 — z3z4 ≥ 30, z5+ ≤ 15, z1z2 ≤ 50 → threshold.
        # Old centroid-distance heuristic mislabelled this "unique".
        self.assertEqual(classify_distribution(30.0, 60.0, 10.0), "threshold")

    def test_polarization_index_above_2_overrides_to_polarized(self):
        # Treff's research-grounded primary criterion: PI > 2.0 → polarized,
        # regardless of where the point lies in centroid space.
        # 90/0.5/9.5 → PI ≈ log10((90+9.5)/0.5) = log10(199) ≈ 2.30.
        pi = polarization_index(90.0, 0.5, 9.5)
        self.assertGreater(pi, 2.0)
        self.assertEqual(classify_distribution(90.0, 0.5, 9.5, pi), "polarized")


class TestPolarizationBlock(unittest.TestCase):

    def test_compute_block_from_user_ride_zone_dict(self):
        # User's May 1 ride zone seconds reproduced exactly. Under the
        # v1.8.0 PI-band cascade this fell through every rule and
        # landed on "unique". v1.8.3 added a moderate-pyramid branch
        # (z3z4 >= 20 AND z3z4 > z5+ AND 40 <= z1z2 < 70 → pyramidal)
        # so the same ride now classifies as "pyramidal" — matching
        # ICU's FastFitness.Tips UI which calls 47.9/34.2/17.9 a
        # textbook pyramid (Z1+Z2 base, smaller Z3+Z4 middle, tiny
        # Z5+ peak).
        tiz = {
            "z1": 2311, "z2": 1952, "z3": 1796, "z4": 1251,
            "z5": 698, "z6": 617, "z7": 283,
        }
        block = compute_polarization_block(tiz)
        self.assertIsNotNone(block)
        self.assertAlmostEqual(block["z1z2_pct"], 47.9, places=1)
        self.assertAlmostEqual(block["z3z4_pct"], 34.2, places=1)
        self.assertAlmostEqual(block["z5plus_pct"], 17.9, places=1)
        # Treff/FastFitness PI for this distribution rounds to 0.28.
        self.assertAlmostEqual(block["polarization_index"], 0.28, places=2)
        # v1.8.3: now matches ICU's pyramidal label.
        self.assertEqual(block["classification"], "pyramidal")
        # Confidence is surfaced in the block for the UI to render.
        self.assertIn("confidence", block)
        self.assertGreaterEqual(block["confidence"], 0.0)
        self.assertLessEqual(block["confidence"], 1.0)

    def test_empty_or_zero_dict_returns_none(self):
        self.assertIsNone(compute_polarization_block(None))
        self.assertIsNone(compute_polarization_block({}))
        self.assertIsNone(compute_polarization_block(
            {f"z{i}": 0 for i in range(1, 8)}
        ))


if __name__ == "__main__":
    unittest.main()
