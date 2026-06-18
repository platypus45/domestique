"""v2.2 — K1: DFA α1 confidence flag + floor-semantics fix + sport gate.

Two user complaints, two root causes:
  * "drops <0.3 during a Gimenez" — α1 genuinely collapses in hard intervals;
    the bug was the per-window 0.30 floor DISCARDING it (a gap, not a low value).
    Fix: per-window floor 0.20 (DFA_WINDOW_SANITY_MIN), whole-ride mean stays 0.30.
  * "easy run reads <=0.5, untrusted" — running RR jitter; fix is to LABEL it
    low/medium confidence (dfa_alpha1_confidence), never silently trust it.
(d) the running artifact-rejection algorithm itself is deferred — validating it
needs real running RR data; the confidence flag is the synthetic-testable fix.
"""
import unittest

import analytics as A
import fit_activity as F


class TestDfaConfidence(unittest.TestCase):
    def test_clean_cycling_is_high(self):
        self.assertEqual(A._dfa_confidence(1.0, 0.9, "cycling"), "high")
        self.assertEqual(A._dfa_confidence(0.0, 1.0, None), "high")

    def test_high_artifact_pct_is_low(self):
        self.assertEqual(A._dfa_confidence(7.5, 0.9, "cycling"), "low")  # >5% Malik

    def test_low_window_yield_caps(self):
        self.assertEqual(A._dfa_confidence(0.0, 0.4, "cycling"), "low")     # <0.5
        self.assertEqual(A._dfa_confidence(0.0, 0.6, "cycling"), "medium")  # <0.75

    def test_running_caps_at_medium_not_disabled(self):
        # a clean running ride is labeled medium (not high, not low/disabled)
        self.assertEqual(A._dfa_confidence(0.0, 1.0, "running"), "medium")
        # ...but a corrupt running ride is still low (worst-of-three)
        self.assertEqual(A._dfa_confidence(9.0, 1.0, "running"), "low")


class TestFloorSemantics(unittest.TestCase):
    def test_per_window_floor_is_below_mean_floor(self):
        # The whole point: per-window α1 may legitimately read below the
        # whole-ride-mean floor (a hard interval), so a Gimenez set is
        # representable instead of discarded.
        self.assertLess(A.DFA_WINDOW_SANITY_MIN, A.DFA_SANITY_MIN)
        self.assertEqual(A.DFA_SANITY_MIN, 0.30)  # mean floor unchanged

    def test_core_returns_confidence_inputs(self):
        import random
        random.seed(42)
        rr, v = [], 0.90  # brownian-ish RR → valid α1 windows (deterministic)
        for _ in range(2500):
            v = max(0.55, min(1.25, v + random.gauss(0, 0.012)))
            rr.append(v)
        out = A.compute_dfa_alpha1(rr)
        self.assertGreater(out["n_windows"], 0, "fixture produced no valid windows")
        self.assertIn("artifact_pct", out)
        self.assertIn("window_yield", out)
        self.assertIsInstance(out["artifact_pct"], (int, float))
        self.assertTrue(0.0 <= out["window_yield"] <= 1.0)


class TestSportReaderRobust(unittest.TestCase):
    def test_missing_file_returns_none(self):
        from pathlib import Path
        self.assertIsNone(F.read_session_sport(Path("/nonexistent/x.fit")))


if __name__ == "__main__":
    unittest.main()
