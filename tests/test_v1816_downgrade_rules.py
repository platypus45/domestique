"""v1.8.16 — DFA cap + aerobic-decoupling downgrade-rule gating.

Live bug: a 5-day-old 9.9% decoupling reading recommended downgrading a hard
session to Z2 while TSB was +17 (fresh), readiness GOOD, DFA healthy (α1=1.126).

Locked rules (MASTER_DECISIONS_v1816):
  - Decoupling (WEAK): advise only if source ride ≤2d old AND not (fresh form
    corroborated by healthy DFA). DFA absent → never veto on form alone.
  - DFA cap (STRONG): recency-gate on newest DFA ride ≤2d; NEVER form-vetoed;
    unknown age → keep the cap (fail-safe).
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from readiness import check_aerobic_decoupling, check_dfa_stress_cap  # noqa: E402


class TestDecouplingGating(unittest.TestCase):
    def test_stale_source_ride_no_advisory(self):
        # v3.6.0 — the v1.8.16 bug was a stale reading advising AS IF CURRENT
        # ("Recent ride ... Z2 recommended" from a days-old number). The
        # original fix truncated at 2 days, which also silenced the signal
        # after a single rest day. The bug is now fixed by LABELLING instead:
        # past the outer window it is still dropped, and inside it the age
        # travels with the advisory so it can never read as this morning's.
        # Verified before the change: `decoupling_advisory` never reaches
        # training_planner.py — this gates a message, not a plan change.
        out = check_aerobic_decoupling(9.9, source_age_days=15)
        self.assertFalse(out["advisory"])
        self.assertIn("stale", out["reason"])
        self.assertEqual(out["confidence"], "stale")

    def test_aging_reading_is_shown_but_labelled_with_its_age(self):
        """The actual v1.8.16 protection: a 5-day-old reading must never
        present itself as current."""
        out = check_aerobic_decoupling(9.9, source_age_days=5)
        self.assertTrue(out["advisory"])
        self.assertEqual(out["confidence"], "aging")
        self.assertIn("5d ago", out["reason"])
        self.assertNotIn("Recent ride", out["reason"])

    def test_fresh_reading_still_reads_as_recent(self):
        out = check_aerobic_decoupling(9.9, source_age_days=1)
        self.assertTrue(out["advisory"])
        self.assertEqual(out["confidence"], "fresh")
        self.assertIn("Recent ride", out["reason"])

    def test_form_veto_no_longer_silences_an_aging_reading(self):
        """A wider window that good form still vetoes would be pointless — the
        cases it exists to surface are exactly the ones TSB would silence."""
        fresh = check_aerobic_decoupling(
            9.9, source_age_days=1, tsb=12.0, dfa_present_and_healthy=True)
        self.assertFalse(fresh["advisory"])          # fresh + good form → vetoed
        aging = check_aerobic_decoupling(
            9.9, source_age_days=6, tsb=12.0, dfa_present_and_healthy=True)
        self.assertTrue(aging["advisory"])           # aging → still surfaced
        self.assertEqual(aging["confidence"], "aging")

    def test_recent_not_fresh_advises(self):
        # Yesterday's decoupled ride, form NOT fresh, no DFA → advisory fires.
        out = check_aerobic_decoupling(
            9.9, source_age_days=1, tsb=-5.0, readiness_status="MODERATE",
            dfa_present_and_healthy=False,
        )
        self.assertTrue(out["advisory"])
        self.assertEqual(out["decoupling_pct"], 9.9)

    def test_fresh_form_with_healthy_dfa_vetoes(self):
        # Fresh TSB + healthy DFA corroboration → suppress the weak signal.
        out = check_aerobic_decoupling(
            9.9, source_age_days=1, tsb=17.0, readiness_status="GOOD",
            dfa_present_and_healthy=True,
        )
        self.assertFalse(out["advisory"])
        self.assertIn("vetoed_by_form", out["reason"])

    def test_fresh_form_but_dfa_absent_keeps_advisory(self):
        # THE CRUX — fresh TSB but NO DFA corroboration (the common case, most
        # rides have no RR). Decoupling is then the only acute signal and TSB
        # lags ~7d, so it must NOT be vetoed on form alone.
        out = check_aerobic_decoupling(
            9.9, source_age_days=1, tsb=17.0, readiness_status="GOOD",
            dfa_present_and_healthy=False,
        )
        self.assertTrue(out["advisory"],
                        "decoupling must survive when DFA can't corroborate freshness")

    def test_below_threshold_no_advisory(self):
        out = check_aerobic_decoupling(3.0, source_age_days=1)
        self.assertFalse(out["advisory"])

    def test_unknown_age_keeps_advisory(self):
        # Fail toward warning when recency is unknown (and form not fresh).
        out = check_aerobic_decoupling(9.9, source_age_days=None,
                                       tsb=-2.0, readiness_status="MODERATE")
        self.assertTrue(out["advisory"])


class TestDfaCapGating(unittest.TestCase):
    def test_cap_fires_when_recent_and_low(self):
        out = check_dfa_stress_cap([0.4, 0.4, 0.45], newest_age_days=1)
        self.assertTrue(out["cap_applied"])

    def test_cap_NOT_vetoed_by_fresh_tsb(self):
        # MANDATORY regression — the STRONG cap has no TSB/form parameter at
        # all; a paper-fresh athlete with collapsing α1 must still be capped.
        out = check_dfa_stress_cap([0.4, 0.4, 0.4], newest_age_days=1)
        self.assertTrue(out["cap_applied"])
        self.assertLess(out["mean_alpha1"], 0.5)

    def test_cap_suppressed_when_stale(self):
        # Newest DFA ride is a week old → "sustained stress" claim is stale.
        out = check_dfa_stress_cap([0.4, 0.4, 0.4], newest_age_days=7)
        self.assertFalse(out["cap_applied"])
        self.assertIn("stale", out["reason"])

    def test_cap_kept_when_age_unknown(self):
        # Fail-safe: unknown date must NOT silence the strong safety net.
        out = check_dfa_stress_cap([0.4, 0.4, 0.4], newest_age_days=None)
        self.assertTrue(out["cap_applied"])

    def test_no_cap_when_healthy(self):
        out = check_dfa_stress_cap([1.1, 1.0, 1.2], newest_age_days=1)
        self.assertFalse(out["cap_applied"])

    def test_insufficient_rides(self):
        out = check_dfa_stress_cap([0.4], newest_age_days=1)
        self.assertEqual(out["reason"], "insufficient_dfa_rides")


class TestDfaCapNotVetoedEndToEnd(unittest.TestCase):
    """The veto must never leak from the weak path onto the strong cap, all
    the way through adjust_today_session."""

    def test_planner_still_caps_fresh_tsb_collapsing_dfa(self):
        import training_planner as tp
        planned = tp.PlannedSession(
            day=None, day_name="Mon", session_type="threshold",
            duration_min=90, tss_estimate=120, description="2x20",
        )
        readiness = {
            "score": 95, "status": "EXCELLENT",  # paper-fresh
            "dfa_cap": check_dfa_stress_cap([0.4, 0.4, 0.4], newest_age_days=1),
            "decoupling_advisory": {"advisory": False},
        }
        adjusted, reason = tp.adjust_today_session(planned, readiness)
        self.assertEqual(adjusted.session_type, "z2")
        self.assertIn("DFA", reason)


if __name__ == "__main__":
    unittest.main()
