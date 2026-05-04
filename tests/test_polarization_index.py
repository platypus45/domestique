"""v4.5.5 IMPL-DETAIL-SERVER / REFINE-CLASSIFY — polarization-index +
centroid-distance classification helpers.

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

Classification (v4.5.5 REFINE-CLASSIFY): closest canonical centroid wins,
with Treff PI > 2.0 acting as a polarized override.
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
        # Z1+Z2 ≥ 90 → base.
        self.assertEqual(classify_distribution(92.0, 6.0, 2.0), "base")

    def test_classify_polarized(self):
        # Z1+Z2 ≥ 75, Z5+ ≥ 5, Z3+Z4 < 20 → polarized.
        self.assertEqual(classify_distribution(78.0, 5.0, 17.0), "polarized")

    def test_classify_pyramidal_z3z4_at_least_z5plus(self):
        # Z3+Z4 ≥ Z5+ AND Z1+Z2 ≥ 65 → pyramidal.
        self.assertEqual(classify_distribution(70.0, 20.0, 10.0), "pyramidal")

    def test_classify_hiit_when_z5plus_above_30(self):
        # Closest centroid for (40, 25, 35) is exactly the HIIT centroid.
        self.assertEqual(classify_distribution(40.0, 25.0, 35.0), "hiit")

    # --- v4.5.5 REFINE-CLASSIFY: centroid-distance method ---

    def test_classify_user_may_1_ride_lands_on_threshold(self):
        # The borderline ride that motivated REFINE-CLASSIFY: distribution
        # 47.9/34.2/17.9 sits closest to the threshold centroid (60,30,10),
        # so the strict-rule "unique" verdict from IMPL-DETAIL-SERVER is gone.
        result = classify_distribution(47.9, 34.2, 17.9)
        self.assertIn(result, {"threshold", "pyramidal"})
        self.assertNotEqual(result, "unique")

    def test_classify_polarized_centroid_high_confidence(self):
        # Distribution exactly on the polarized centroid → confidence ≈ 1.0.
        self.assertEqual(classify_distribution(80.0, 5.0, 15.0), "polarized")
        self.assertGreater(classification_confidence(80.0, 5.0, 15.0), 0.95)

    def test_classify_pyramidal_centroid_high_confidence(self):
        # Distribution exactly on the pyramidal centroid → confidence ≈ 1.0.
        self.assertEqual(classify_distribution(80.0, 15.0, 5.0), "pyramidal")
        self.assertGreater(classification_confidence(80.0, 15.0, 5.0), 0.95)

    def test_classify_hiit_leaning_distribution(self):
        # 40/25/35 is the HIIT centroid itself.
        self.assertEqual(classify_distribution(40.0, 25.0, 35.0), "hiit")

    def test_classify_unique_when_far_from_every_centroid(self):
        # 30/60/10 is >35 away from every canonical centroid (closest is
        # threshold at ≈42), so the classifier falls back to "unique".
        self.assertEqual(classify_distribution(30.0, 60.0, 10.0), "unique")

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
        # v4.5.5 REFINE-CLASSIFY centroid-distance rule, this borderline
        # distribution lands on the "threshold" centroid (60,30,10) instead
        # of the IMPL-DETAIL-SERVER "unique" fallback.
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
        # Closest centroid is "threshold" (60,30,10) at distance ≈15.
        self.assertEqual(block["classification"], "threshold")
        # Confidence is surfaced in the block for the UI to render.
        self.assertIn("confidence", block)
        self.assertGreater(block["confidence"], 0.0)
        self.assertLessEqual(block["confidence"], 1.0)

    def test_empty_or_zero_dict_returns_none(self):
        self.assertIsNone(compute_polarization_block(None))
        self.assertIsNone(compute_polarization_block({}))
        self.assertIsNone(compute_polarization_block(
            {f"z{i}": 0 for i in range(1, 8)}
        ))


if __name__ == "__main__":
    unittest.main()
