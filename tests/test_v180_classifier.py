"""v1.8.0 §F3 — Treff 2019 PI-band classifier verification.

Replaces the centroid-distance heuristic with a top-down PI-band cascade
(see /tmp/MASTER_DECISIONS_v180_addendum.md §F3). The 6 reference cases
in the addendum's verification table MUST all pass.
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


class TestTreffVerificationTable(unittest.TestCase):
    """The 6 reference cases from MASTER_DECISIONS_v180_addendum.md §F3."""

    def test_treff_pyramidal_15_49p2_35p8(self):
        # PI = log10((15+35.8)/49.2) ≈ 0.01. Old centroid classifier flagged
        # this as `hiit @ 1% confidence`; the new cascade reaches pyramidal
        # because z3z4=49.2 is dominant AND z3z4 exceeds z5+ by > 10 points.
        self.assertEqual(classify_distribution(15, 49.2, 35.8), "pyramidal")

    def test_polarized_when_pi_above_2(self):
        # 75/5/20 with PI=4.0 — explicit polarized override.
        self.assertEqual(classify_distribution(75, 5, 20, pi=4.0), "polarized")

    def test_base_when_z1z2_dominant_low_pi(self):
        # 80/15/5 PI=0.5 — z3z4 too small for pyramidal/threshold, z1z2 dominant.
        self.assertEqual(classify_distribution(80, 15, 5, pi=0.5), "base")

    def test_threshold_when_z3z4_dominant_z5_low(self):
        # 30/60/10 — high z3z4, very low z5+, moderate z1z2 → threshold band.
        # Old centroid classifier called this "unique" (distance > 35).
        self.assertEqual(classify_distribution(30, 60, 10, pi=0.3), "threshold")

    def test_hiit_when_z5plus_dominant_z1z2_minimal(self):
        # 10/30/60 PI=0.2 — > 40% z5+ and < 20% z1z2 → hiit.
        self.assertEqual(classify_distribution(10, 30, 60, pi=0.2), "hiit")

    def test_unique_balanced_third_each(self):
        # 33/33/34 — no band rule matches → unique fallback.
        self.assertEqual(classify_distribution(33, 33, 34, pi=0.05), "unique")


class TestPIBandPrecedence(unittest.TestCase):
    """PI > 2.0 wins over every band rule. Other bands evaluated top-down."""

    def test_pi_above_2_polarized_overrides(self):
        # Even if shape would otherwise score pyramidal/threshold, PI > 2 wins.
        # 90/0.5/9.5 → PI = log10(99.5/0.5) ≈ 2.30.
        pi = polarization_index(90.0, 0.5, 9.5)
        self.assertGreater(pi, 2.0)
        self.assertEqual(classify_distribution(90.0, 0.5, 9.5, pi), "polarized")

    def test_threshold_evaluated_before_pyramidal(self):
        # 30/60/10 satisfies BOTH the pyramidal rule (z3z4>=35, z3z4>z5+10)
        # AND the threshold rule. Threshold wins because it's tighter and
        # is checked first in the cascade.
        self.assertEqual(classify_distribution(30, 60, 10, pi=0.3), "threshold")

    def test_classify_works_without_explicit_pi(self):
        # When `pi` is omitted, it's computed internally from the percentages.
        self.assertEqual(classify_distribution(15, 49.2, 35.8), "pyramidal")
        self.assertEqual(classify_distribution(10, 30, 60), "hiit")


class TestClassificationConfidence(unittest.TestCase):
    """Confidence is PI-distance from band centre, clamped to [0.5, 1.0]
    for in-band rides. Unique returns 0.5 (edge boundary)."""

    def test_unique_returns_half(self):
        self.assertEqual(classification_confidence(33, 33, 34, pi=0.05), 0.5)

    def test_in_band_confidence_at_least_half(self):
        # Every Treff verification case (except `unique`) confidence ≥ 0.5.
        for z1z2, z3z4, z5p, pi in [
            (15, 49.2, 35.8, 0.01),
            (75, 5, 20, 4.0),
            (80, 15, 5, 0.5),
            (30, 60, 10, 0.3),
            (10, 30, 60, 0.2),
        ]:
            conf = classification_confidence(z1z2, z3z4, z5p, pi=pi)
            self.assertGreaterEqual(conf, 0.5,
                f"({z1z2}/{z3z4}/{z5p}) PI={pi} conf={conf} < 0.5")
            self.assertLessEqual(conf, 1.0)

    def test_polarized_band_confidence_scales_with_pi(self):
        # Addendum formula: confidence = min(1.0, (pi-2.0)/2.0 + 0.5).
        # PI=2.01 → ≈ 0.505 (at edge). PI=4.0 → 1.0 (well into the band).
        edge = classification_confidence(75, 5, 20, pi=2.01)
        deep = classification_confidence(75, 5, 20, pi=4.0)
        self.assertLess(edge, deep)
        self.assertGreaterEqual(edge, 0.5)
        self.assertLessEqual(deep, 1.0)


class TestComputeBlock(unittest.TestCase):
    """`compute_polarization_block` returns the same shape and honours the
    new classifier."""

    def test_block_classification_reflects_pi_band_cascade(self):
        # The Treff ride zones (15/49.2/35.8 from the verification table).
        # Synthesise a TIZ dict that produces those percentages.
        # total = 1000s. z1+z2 = 150, z3+z4 = 492, z5+ = 358.
        tiz = {
            "z1": 75, "z2": 75,
            "z3": 246, "z4": 246,
            "z5": 120, "z6": 120, "z7": 118,
        }
        block = compute_polarization_block(tiz)
        self.assertIsNotNone(block)
        # Allow small rounding tolerance from integer seconds.
        self.assertAlmostEqual(block["z1z2_pct"], 15.0, places=0)
        self.assertAlmostEqual(block["z3z4_pct"], 49.2, places=0)
        self.assertAlmostEqual(block["z5plus_pct"], 35.8, places=0)
        self.assertEqual(block["classification"], "pyramidal")
        self.assertGreaterEqual(block["confidence"], 0.5)
        self.assertLessEqual(block["confidence"], 1.0)


if __name__ == "__main__":
    unittest.main()
