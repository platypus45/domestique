"""v1.6.1 — training_planner observability tests.

Verifies the planner emits the new ``E_*`` codes when its meaty paths
fail. Uses ``unittest.mock.patch`` to inject a synthetic exception into
each target path and asserts the corresponding code lands in the diag
ring buffer (via the ``_LOG_ERROR_HOOK`` registered by ``app.py``).

Why patch app's ring directly + import app: the planner's
``_tp_log_error`` routes through ``app._log_error`` once the hook is
installed, which appends to ``app._DIAG_RING``. So a single import of
``app`` arms the path; subsequent calls to planner internals push onto
that shared ring.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import patch

import app as app_module  # registers the _tp_log_error hook on import
import error_codes as ec
import training_planner as tp


def _ring_codes():
    with app_module._DIAG_RING_LOCK:
        return [e["code"] for e in list(app_module._DIAG_RING)]


def _ring_clear():
    with app_module._DIAG_RING_LOCK:
        app_module._DIAG_RING.clear()


class PhaseDeriveFailureTests(unittest.TestCase):
    def setUp(self):
        _ring_clear()

    def test_generate_phases_failure_logs_E_PHASE_DERIVE_FAILED(self):
        """Patch generate_phases to raise; generate_plan should re-raise
        and log E_PHASE_DERIVE_FAILED.
        """
        # Build a minimal valid Goal (date in future so weeks_available > 0).
        goal = tp.Goal(
            goal_type="general",
            hours_per_week=8.0,
            target_date=date.today() + timedelta(weeks=12),
        )
        with patch.object(tp, "generate_phases",
                          side_effect=RuntimeError("synthetic phase fail")):
            with self.assertRaises(RuntimeError):
                tp.generate_plan(goal)
        codes = _ring_codes()
        self.assertIn(ec.Codes.PHASE_DERIVE_FAILED, codes,
                      f"expected E_PHASE_DERIVE_FAILED in ring, got {codes}")


class PhaseBuildFailureTests(unittest.TestCase):
    def setUp(self):
        _ring_clear()

    def test_plan_week_failure_logs_E_PLAN_PHASE_BUILD_FAILED(self):
        """Patch plan_week to raise inside the for-phase loop; assert the
        per-phase wrapper surfaces E_PLAN_PHASE_BUILD_FAILED with the
        phase name in context.
        """
        goal = tp.Goal(
            goal_type="general",
            hours_per_week=8.0,
            target_date=date.today() + timedelta(weeks=12),
        )
        # Let generate_phases run normally; only fail inside plan_week.
        with patch.object(tp, "plan_week",
                          side_effect=RuntimeError("synthetic plan_week fail")):
            with self.assertRaises(RuntimeError):
                tp.generate_plan(goal)
        codes = _ring_codes()
        self.assertIn(ec.Codes.PLAN_PHASE_BUILD_FAILED, codes,
                      f"expected E_PLAN_PHASE_BUILD_FAILED in ring, got {codes}")
        # Check at least one entry has phase name in ctx
        with app_module._DIAG_RING_LOCK:
            entries = [e for e in app_module._DIAG_RING
                       if e["code"] == ec.Codes.PLAN_PHASE_BUILD_FAILED]
        self.assertTrue(entries, "no E_PLAN_PHASE_BUILD_FAILED entry")
        self.assertIn("phase", entries[-1]["context"])


class ReforecastDictFailureTests(unittest.TestCase):
    def setUp(self):
        _ring_clear()

    def test_reforecast_dict_step_dict_to_planned_weeks_logs(self):
        """Patch _plan_dict_to_planned_weeks to raise; reforecast_dict
        should re-raise + log E_REFORECAST_DICT_FAILED with step ctx.
        """
        plan_dict = {"goal": {"type": "general"}, "weeks": []}
        with patch.object(tp, "_plan_dict_to_planned_weeks",
                          side_effect=RuntimeError("synthetic to-pw fail")):
            with self.assertRaises(RuntimeError):
                tp.reforecast_dict(plan_dict)
        codes = _ring_codes()
        self.assertIn(ec.Codes.REFORECAST_DICT_FAILED, codes,
                      f"expected E_REFORECAST_DICT_FAILED in ring, got {codes}")
        with app_module._DIAG_RING_LOCK:
            entries = [e for e in app_module._DIAG_RING
                       if e["code"] == ec.Codes.REFORECAST_DICT_FAILED]
        self.assertTrue(entries)
        self.assertEqual(entries[-1]["context"].get("step"),
                         "dict_to_planned_weeks")


class MatchZwoNoCandidatesTests(unittest.TestCase):
    def setUp(self):
        _ring_clear()

    def test_empty_library_logs_E_MATCH_ZWO_NO_CANDIDATES(self):
        """Empty library → both pools empty → E_MATCH_ZWO_NO_CANDIDATES."""
        session = tp.PlannedSession(
            day=date.today(), day_name="Mon",
            session_type="z2", duration_min=60, tss_estimate=45,
            description="z2",
        )
        # Empty library guarantees no candidates and no coverage_pool entry.
        tp.match_zwo(session, library=[], week_num=1, day_idx=0)
        codes = _ring_codes()
        self.assertIn(ec.Codes.MATCH_ZWO_NO_CANDIDATES, codes,
                      f"expected E_MATCH_ZWO_NO_CANDIDATES, got {codes}")


class PlanDictMalformedWeekTests(unittest.TestCase):
    """v1.6.1 §C: malformed week skip should emit E_REFORECAST_DICT_TO_PW
    (WARN). Triggered by a week with no ``start`` field.
    """

    def setUp(self):
        _ring_clear()

    def test_malformed_week_logs_REFORECAST_DICT_TO_PW(self):
        plan_dict = {"weeks": [
            # Missing both 'start' and 'end' — should be skipped + logged.
            {"week_num": 1, "phase": "base", "sessions": []},
        ]}
        result = tp._plan_dict_to_planned_weeks(plan_dict)
        # The malformed week is skipped → empty list returned.
        self.assertEqual(result, [])
        codes = _ring_codes()
        self.assertIn(ec.Codes.REFORECAST_DICT_TO_PW, codes,
                      f"expected E_REFORECAST_DICT_TO_PW, got {codes}")


if __name__ == "__main__":
    unittest.main()
