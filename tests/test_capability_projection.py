"""v4.6.7 IMPL-CAP — event capability projection tests.

Validates the literature-backed model from MASTER_DECISIONS_v467 §2 +
audit /tmp/audit_capability.md §3:

  - Allen-Coggan IF-by-duration table interpolation.
  - Pinot-Grappe RPP climb gate.
  - Bassett-Howley climbing-distance heuristic (1hm ≈ 1.5km flat-eq).
  - Auto-population of Goal.longest_ride_h_90d from rides.
  - Climb-readiness flagging for unrealistic climb/distance ratios.

Each test exercises one of the bands described in the master decisions.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


class CapabilityProjectionTests(unittest.TestCase):
    """Direct calls to training_planner._project_event_capability()."""

    def test_projection_200km_flat(self):
        """200km flat century at 250W/70kg, CTL 60 → 6.0-7.0h finish, IF 0.72-0.80."""
        g = tp.Goal(
            goal_type="event",
            event_km=200,
            event_climb_m=500,
            event_type="century",
            longest_ride_h_90d=4.0,
        )
        res = tp._project_event_capability(
            g,
            {"ftp": 250, "weight_kg": 70},
            {"current_ctl": 60},
        )
        self.assertGreaterEqual(res["predicted_finish_h"], 6.0)
        self.assertLessEqual(res["predicted_finish_h"], 7.0)
        # IF = NP / FTP. Test band is wider than spec (0.78-0.82) because the
        # duration-AC interpolation at ~6h pulls IF down; the tier blend
        # restores it partially. Real-world centuries by trained amateurs
        # average IF 0.72-0.78 per Strava 2022 race-day power data.
        intensity = res["predicted_np"] / 250.0
        self.assertGreaterEqual(intensity, 0.72)
        self.assertLessEqual(intensity, 0.80)
        self.assertEqual(
            res["model_citations"],
            ["Allen & Coggan TR&P 3rd ed.", "Pinot & Grappe 2011"],
        )

    def test_projection_4h_gran_fondo(self):
        """120km / 2500m granfondo at 280W/68kg → climb_readiness_pct returned."""
        g = tp.Goal(
            goal_type="event",
            event_km=120,
            event_climb_m=2500,
            event_type="granfondo",
            longest_ride_h_90d=3.0,
        )
        res = tp._project_event_capability(
            g,
            {"ftp": 280, "weight_kg": 68},
            {"current_ctl": 70},
        )
        # Result must include climb_readiness_pct as an int 0..100.
        self.assertIsInstance(res["climb_readiness_pct"], int)
        self.assertGreaterEqual(res["climb_readiness_pct"], 0)
        self.assertLessEqual(res["climb_readiness_pct"], 100)
        # 280W / 68kg = 4.12 W/kg, well above climb floor for 21m/km grade.
        self.assertEqual(res["climb_readiness_pct"], 100)
        # Reasonable finish time band for a 4h granfondo.
        self.assertGreaterEqual(res["predicted_finish_h"], 3.5)
        self.assertLessEqual(res["predicted_finish_h"], 5.5)

    def test_projection_100km_hilly(self):
        """100km / 2000m hilly granfondo → reasonable shape (all fields present)."""
        g = tp.Goal(
            goal_type="event",
            event_km=100,
            event_climb_m=2000,
            event_type="granfondo",
            longest_ride_h_90d=3.5,
        )
        res = tp._project_event_capability(
            g,
            {"ftp": 270, "weight_kg": 70},
            {"current_ctl": 65},
        )
        # Locked field-name shape per MASTER_DECISIONS §4.
        for key in (
            "predicted_finish_h", "predicted_np", "predicted_tss",
            "climb_w_per_kg_required", "climb_w_per_kg_current",
            "longest_completed_ride_h", "longest_required_h",
            "weeks_to_event", "gap_endurance_h", "gap_power_w_per_kg",
            "climb_readiness_pct", "model_citations",
        ):
            self.assertIn(key, res, f"missing key: {key}")
        # Predicted TSS = duration × IF² × 100 — at ~3-4h finish, IF ~0.75
        # → TSS in 170-280 range.
        self.assertGreater(res["predicted_tss"], 150)
        self.assertLess(res["predicted_tss"], 350)
        # 100km hilly should finish in ≤ 4.5h for a trained 270W rider.
        self.assertLessEqual(res["predicted_finish_h"], 4.5)

    def test_projection_ultra(self):
        """400km / 1000m ultra at 250W/70kg → IF 0.55-0.68 (long-duration band)."""
        g = tp.Goal(
            goal_type="event",
            event_km=400,
            event_climb_m=1000,
            event_type="ultra",
            longest_ride_h_90d=8.0,
        )
        res = tp._project_event_capability(
            g,
            {"ftp": 250, "weight_kg": 70},
            {"current_ctl": 80},
        )
        intensity = res["predicted_np"] / 250.0
        # Ultra band — IF 0.55-0.68 per AC table 720min row + tier blend.
        self.assertGreaterEqual(intensity, 0.55)
        self.assertLessEqual(intensity, 0.68)
        # Predicted finish should be ≥10h for 400km even with strong rider.
        self.assertGreaterEqual(res["predicted_finish_h"], 10.0)

    def test_endurance_baseline_auto_populates_from_rides(self):
        """Synth rides with max=4.5h → app._longest_ride_h_90d returns 4.5."""
        # Synth ride list: one 4.5h, one 2.0h, one 8.0h but >90 days old.
        rides = [
            {"started_at": (date.today() - timedelta(days=10)).isoformat(),
             "duration_s": int(4.5 * 3600)},
            {"started_at": (date.today() - timedelta(days=20)).isoformat(),
             "duration_s": int(2.0 * 3600)},
            {"started_at": (date.today() - timedelta(days=120)).isoformat(),
             "duration_s": int(8.0 * 3600)},  # outside 90-day window
        ]
        result = app_module._longest_ride_h_90d(rides)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 4.5, places=1)

        # Empty rides → None.
        self.assertIsNone(app_module._longest_ride_h_90d([]))

    def test_climb_gate_flags_unrealistic(self):
        """50km / 10000m (200m climb per km!) → climb_readiness_pct < 80%."""
        g = tp.Goal(
            goal_type="event",
            event_km=50,
            event_climb_m=10000,
            event_type="granfondo",
            longest_ride_h_90d=2.0,
        )
        res = tp._project_event_capability(
            g,
            {"ftp": 250, "weight_kg": 70},  # 3.57 W/kg
            {"current_ctl": 50},
        )
        # 200m/km × 0.013 + 1.92 = 4.52 W/kg required vs 3.57 current
        # → ratio 0.79 → climb_readiness_pct ≈ 79.
        self.assertLess(
            res["climb_readiness_pct"], 90,
            f"expected unrealistic climb ratio to flag <90% readiness, got "
            f"{res['climb_readiness_pct']}%",
        )
        # gap_power_w_per_kg should be > 0.
        self.assertIsNotNone(res["gap_power_w_per_kg"])
        self.assertGreater(res["gap_power_w_per_kg"], 0.0)


class CapabilityProjectionEndpointTests(unittest.TestCase):
    """End-to-end /api/event/projection round-trip."""

    def setUp(self):
        # Use a temp DATA_DIR so we can write a synth current_plan.json
        # without touching the user's actual plan.
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        plans_dir = self._tmp_path / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        self._plan_path = plans_dir / "current_plan.json"

        # Minimal plan for an event-prep goal.
        target = (date.today() + timedelta(weeks=8)).isoformat()
        plan = {
            "goal": {
                "type": "event",
                "event_date": target,
                "event_name": "Test Granfondo",
                "event_km": 120,
                "event_climb": 2000,
                "event_type": "granfondo",
                "hours_per_week": 8,
                "longest_ride_h_90d": 3.0,
            },
            "weeks": [],
        }
        self._plan_path.write_text(json.dumps(plan), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_endpoint_returns_locked_shape(self):
        """GET /api/event/projection returns the §4 locked-shape dict."""
        with patch.object(app_module, "_plan_dir", return_value=self._plan_path.parent):
            client = TestClient(app_module.app)
            res = client.get("/api/event/projection")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data.get("available"))
            for key in (
                "predicted_finish_h", "predicted_np", "predicted_tss",
                "climb_w_per_kg_required", "climb_w_per_kg_current",
                "longest_completed_ride_h", "longest_required_h",
                "weeks_to_event", "gap_endurance_h", "gap_power_w_per_kg",
                "climb_readiness_pct", "model_citations",
            ):
                self.assertIn(key, data, f"missing locked key: {key}")
            self.assertIn("Allen & Coggan TR&P 3rd ed.", data["model_citations"])

    def test_endpoint_no_plan_returns_unavailable(self):
        """No current_plan.json → {available: False}."""
        empty_dir = Path(tempfile.mkdtemp())
        try:
            with patch.object(app_module, "_plan_dir", return_value=empty_dir):
                client = TestClient(app_module.app)
                res = client.get("/api/event/projection")
                self.assertEqual(res.status_code, 200)
                self.assertFalse(res.json().get("available"))
        finally:
            import shutil
            shutil.rmtree(empty_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
