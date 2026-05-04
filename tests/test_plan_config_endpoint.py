"""Wave-1C IMPL-PLAN-CONFIG-UI (v4.6.0) — regression tests for the new
``/api/plan/preview`` endpoint that drives the Plan Overview right-side
panel in dashboard.html.

Bug: the right panel previously rendered phases from ``current_plan.json``
(the LAST GENERATED plan), so a user who typed PLAN WEEKS=20 saw a stale
14-week phase split (BASE 8 + BUILD1 4 + TAPER 2) that ignored their
input. The generated grid below it then expanded to 20 weeks with
BUILD2 + PEAK as well — visible mismatch.

Fix: ``/api/plan/preview`` calls the SAME ``tp.generate_phases`` used by
``/api/plan/generate``, so both surfaces share a single source of truth.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from fastapi.testclient import TestClient

import app as app_module


class PlanPreviewEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app_module.app)

    def _phases_sum(self, phases: list[dict]) -> int:
        return sum(int(p.get("weeks", 0) or 0) for p in phases)

    def test_general_goal_20_weeks_returns_phases_summing_to_20(self) -> None:
        """goal=general, plan_weeks=20 → phases sum to 20."""
        r = self.client.get("/api/plan/preview",
                            params={"goal": "general", "plan_weeks": 20})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d.get("ok"))
        phases = d["phases"]
        self.assertGreater(len(phases), 0)
        # General has no taper but should still cover 20 weeks across
        # base + build1 + build2 + peak.
        self.assertEqual(self._phases_sum(phases), 20,
                         f"phase sum != 20: {phases}")

    def test_event_prep_20_weeks_returns_5_phases_summing_to_20(self) -> None:
        """goal=event, event ~140 days out, plan_weeks=20 → 5 phases sum=20."""
        event_date = (date.today() + timedelta(days=140)).isoformat()
        r = self.client.get("/api/plan/preview",
                            params={"goal": "event",
                                    "plan_weeks": 20,
                                    "event_date": event_date})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        phases = d["phases"]
        names = [p["name"] for p in phases]
        # Event-prep at 20 weeks should produce all 5 phases per
        # MASTER §3 Pillar C: BASE + BUILD1 + BUILD2 + PEAK + TAPER.
        self.assertEqual(len(phases), 5,
                         f"expected 5 phases, got {len(phases)}: {names}")
        self.assertIn("base", names)
        self.assertIn("build1", names)
        self.assertIn("build2", names)
        self.assertIn("peak", names)
        self.assertIn("taper", names)
        self.assertEqual(self._phases_sum(phases), 20,
                         f"phase sum != 20: {phases}")

    def test_preview_matches_generate_phase_counts(self) -> None:
        """/api/plan/preview must produce the same phase split as
        /api/plan/generate uses internally (single source of truth).

        We compare against ``training_planner.generate_phases`` directly
        (which is what /api/plan/generate calls under the hood) so we
        don't need to actually generate + persist a full plan in the
        test environment.
        """
        import training_planner as tp

        event_date = (date.today() + timedelta(days=140)).isoformat()
        r = self.client.get("/api/plan/preview",
                            params={"goal": "event",
                                    "plan_weeks": 20,
                                    "event_date": event_date})
        self.assertEqual(r.status_code, 200)
        preview_phases = r.json()["phases"]

        g = tp.Goal(
            goal_type="event",
            target_date=date.fromisoformat(event_date),
            plan_weeks=20,
            hours_per_week=8.0,
        )
        # Use the same default current_ctl fallback as /api/plan/preview
        # uses when training metrics are unavailable in the test runner.
        try:
            from training import get_today_metrics
            m = get_today_metrics() or {}
            current_ctl = float(m.get("ctl") or 37.0)
        except Exception:
            current_ctl = 37.0
        ref_phases = tp.generate_phases(g, current_ctl)

        self.assertEqual(len(preview_phases), len(ref_phases),
                         "phase counts diverged between preview and "
                         "training_planner.generate_phases")
        self.assertEqual([p["name"] for p in preview_phases],
                         [p.name for p in ref_phases])
        self.assertEqual([p["weeks"] for p in preview_phases],
                         [p.weeks for p in ref_phases])

    def test_event_prep_20_weeks_includes_build2_and_peak(self) -> None:
        """B2 specific regression: 20-week event-prep right panel was
        missing BUILD2 + PEAK. Verify both are present alongside BASE,
        BUILD1, TAPER (which were the only 3 phases shown previously)."""
        event_date = (date.today() + timedelta(days=140)).isoformat()
        r = self.client.get("/api/plan/preview",
                            params={"goal": "event",
                                    "plan_weeks": 20,
                                    "event_date": event_date})
        self.assertEqual(r.status_code, 200)
        names = [p["name"] for p in r.json()["phases"]]
        # The bug-state set was {base, build1, taper}; assert the two
        # missing phases are now present in addition to the original 3.
        self.assertIn("build2", names,
                      f"build2 missing from 20-week event-prep preview: {names}")
        self.assertIn("peak", names,
                      f"peak missing from 20-week event-prep preview: {names}")
        # Sanity: original 3 still present.
        self.assertIn("base", names)
        self.assertIn("build1", names)
        self.assertIn("taper", names)


if __name__ == "__main__":
    unittest.main()
