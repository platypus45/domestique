"""v1.8.3 BUG-B — moderate-pyramid classifier branch.

ICU's FastFitness.Tips classifies the user's ride (58.3 / 27.6 / 14.1)
as Pyramidaal; Domestique returned 'unique' because the strict
z3z4 >= 35 rule didn't match. v1.8.3 adds a moderate-pyramid branch
before the final unique fallback:

    z3z4 >= 20 AND z3z4 > z5+ AND 40 <= z1z2 < 70 → pyramidal

These tests pin down the boundary behaviour of the new rule and
confirm it doesn't perturb adjacent cascade rules.
"""
from __future__ import annotations

import unittest

from analytics import classify_distribution


class TestModeratePyramidClassifier(unittest.TestCase):
    """The new moderate-pyramid rule + its boundary behaviour."""

    def test_user_reported_case_58p3_27p6_14p1(self):
        # The exact ride from the bug report. ICU calls it Pyramidaal.
        # PI = log10((58.3+14.1)/27.6) ≈ log10(2.62) ≈ 0.418, far below 2.0.
        # Strict pyramidal rule (z3z4 >= 35) doesn't match (27.6 < 35).
        # Moderate pyramidal rule catches it:
        #   z3z4=27.6 >= 20  AND  27.6 > 14.1  AND  40 <= 58.3 < 70.
        self.assertEqual(
            classify_distribution(58.3, 27.6, 14.1, 1.47),
            "pyramidal",
        )

    def test_z3z4_equals_z5plus_falls_through(self):
        # (40.0, 20.0, 40.0): z3z4 (20) is NOT > z5+ (40), so the
        # moderate-pyramid rule fails. None of the earlier rules match
        # either (z3z4 < 35, z3z4 < 30, z1z2 < 70, z1z2 not < 20).
        # PI = log10((40+40)/20) = log10(4) ≈ 0.602, not > 2.0.
        # Expected: unique.
        self.assertEqual(
            classify_distribution(40.0, 20.0, 40.0, 0.602),
            "unique",
        )

    def test_z1z2_just_below_70_is_moderate_pyramid(self):
        # (69.9, 20.1, 10.0): z1z2 just under the base cutoff.
        # Moderate-pyramid rule:
        #   z3z4=20.1 >= 20  AND  20.1 > 10  AND  40 <= 69.9 < 70.
        self.assertEqual(
            classify_distribution(69.9, 20.1, 10.0, 0.602),
            "pyramidal",
        )

    def test_z1z2_at_70_is_base_not_pyramid(self):
        # (70.0, 20.0, 10.0): z1z2 reaches the base cutoff. The base
        # rule (z1z2 >= 70) is checked BEFORE the moderate-pyramid rule
        # in the cascade, so base wins. The moderate-pyramid rule's
        # z1z2 < 70 clause would also exclude this case if base didn't
        # catch it first.
        self.assertEqual(
            classify_distribution(70.0, 20.0, 10.0, 0.602),
            "base",
        )

    def test_high_z5plus_low_z3z4_unaffected_by_new_rule(self):
        # (30.0, 10.0, 60.0): z3z4 (10) is below the moderate-pyramid
        # threshold (20), so the new rule cannot pull this into
        # pyramidal. The strict hiit rule needs z1z2 < 20 (30 fails),
        # threshold needs z3z4 >= 30 (10 fails), strict pyramidal
        # needs z3z4 >= 35 (10 fails), base needs z1z2 >= 70 (30 fails).
        # Result: unique. The point of this test is to confirm the new
        # rule does NOT incorrectly classify this as pyramidal.
        result = classify_distribution(30.0, 10.0, 60.0, 0.954)
        self.assertNotEqual(result, "pyramidal")
        self.assertEqual(result, "unique")


if __name__ == "__main__":
    unittest.main()
