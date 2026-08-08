"""v4.5.1 FIX-SERVER §B2 — _classify_card_state checks has_actual before
session_type=="rest".

Before the fix: a rest day with an attached actual ride classified as
"rest", and the UI suppressed the click handler / dropped the actual
cell. Result: an unplanned ride on a rest day was effectively invisible
in the calendar.

After: completed > rest > missing_workout > planned. has_actual short-
circuits regardless of planner session_type.

Three cases:
  1. session_type="rest" + has_actual=True  → "completed"
  2. session_type="rest" + has_actual=False → "rest"
  3. session_type="z2"   + has_actual=True  → "completed"
"""
from __future__ import annotations

import unittest

import app as app_module


class TestCardStateRestWithActual(unittest.TestCase):
    """v4.5.1 §B2 — has_actual takes precedence over rest classification."""

    def test_rest_with_actual_returns_completed(self):
        # Case 1: planned rest day, but a ride matched anyway → completed.
        session = {"session_type": "rest", "zwo_file": ""}
        state = app_module._classify_card_state(session, has_actual=True, library_lookup=None)
        self.assertEqual(state, "completed",
                         "rest day with attached actual must classify as completed")

    def test_rest_no_actual_returns_rest(self):
        # Case 2: planned rest day, no ride matched → still rest.
        session = {"session_type": "rest", "zwo_file": ""}
        state = app_module._classify_card_state(session, has_actual=False, library_lookup=None)
        self.assertEqual(state, "rest",
                         "rest day with no actual must classify as rest")

    def test_planned_with_actual_returns_completed(self):
        # Case 3: planned z2 day, ride present → completed (regression check).
        session = {"session_type": "z2", "zwo_file": "z2_steady_68pct_120min.zwo"}
        state = app_module._classify_card_state(session, has_actual=True, library_lookup=None)
        self.assertEqual(state, "completed",
                         "planned day with attached actual must classify as completed")


if __name__ == "__main__":
    unittest.main()
