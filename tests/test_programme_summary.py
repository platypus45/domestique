"""v4.6.7 IMPL-SUM — programme summary endpoint + PNG renderer tests.

Five tests cover the §4 contract:
  1. test_endpoint_returns_top12_keys           — response shape
  2. test_ftp_delta_computation                 — ledger 240→260 ⇒ +8.3%
  3. test_intensity_distribution_sums_to_total  — zone-time aggregation
  4. test_compliance_per_phase                  — one entry per phase
  5. test_png_render_returns_image_bytes        — PNG header check

References cited in MASTER_DECISIONS_v467.md §4: Stöggl 2014, Foster 1998,
Treff 2019, Hooper 1995, Coggan/Allen TR&P.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


def _mk_synth_plan(start_date: date, weeks: int = 4) -> dict:
    """Minimal plan dict with the fields the summary endpoint reads."""
    end_date = start_date + timedelta(days=weeks * 7 - 1)
    weeks_list = []
    for w in range(weeks):
        ws = start_date + timedelta(days=w * 7)
        we = ws + timedelta(days=6)
        weeks_list.append({
            "week_num": w + 1,
            "start": ws.isoformat(),
            "end": we.isoformat(),
            "phase": "base" if w < 2 else "build1",
            "tss_target": 300,
            "is_stepback": False,
            "sessions": [],
        })
    return {
        "goal": {"type": "general", "hours_per_week": 8.0},
        "phases": [
            {"name": "base", "weeks": 2, "start": start_date.isoformat(),
             "end": (start_date + timedelta(days=13)).isoformat(),
             "weekly_tss": 300, "focus": "Aerobic base"},
            {"name": "build1", "weeks": 2,
             "start": (start_date + timedelta(days=14)).isoformat(),
             "end": end_date.isoformat(),
             "weekly_tss": 400, "focus": "Sweet spot"},
        ],
        "weeks": weeks_list,
        "generated": start_date.isoformat(),
    }


class ProgrammeSummaryEndpointTests(unittest.TestCase):
    """Endpoint shape + key metrics — uses TestClient with synth plan."""

    def setUp(self) -> None:
        # Import inside setUp so the temp plan dir override applies
        # before app sees /api/plan paths.
        import app as app_module
        self.app_module = app_module
        self.client = TestClient(app_module.app)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        # Patch _plan_dir to point at our tmp.
        self._patch = patch.object(
            app_module, "_plan_dir", lambda: self.tmp_path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def _write_plan(self, plan: dict) -> None:
        (self.tmp_path / "current_plan.json").write_text(
            json.dumps(plan), encoding="utf-8")

    def test_endpoint_returns_top12_keys(self) -> None:
        """GET /api/programme/summary returns all §4 contract keys."""
        plan = _mk_synth_plan(date.today() - timedelta(days=70), weeks=8)
        self._write_plan(plan)

        r = self.client.get("/api/programme/summary",
                            params={"plan_id": "test"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()

        # All §4 keys present.
        for key in ("plan_id", "start_date", "end_date", "weeks",
                    "ftp_delta", "eftp_delta", "vo2max_delta", "ctl_gain",
                    "intensity_dist", "pol_index", "monotony_max",
                    "strain_max", "compliance", "mean_max_curve",
                    "hooper_trend", "totals", "decoupling_trend",
                    "citations"):
            self.assertIn(key, d, f"missing §4 key: {key}")
        self.assertEqual(d["plan_id"], "test")
        self.assertEqual(d["weeks"], 8)
        # Compliance is a list of {phase, planned_tss, actual_tss, pct}
        self.assertIsInstance(d["compliance"], list)
        # Citations include the literature backbone.
        cites = " ".join(d["citations"])
        self.assertIn("Stöggl", cites)
        self.assertIn("Foster", cites)
        self.assertIn("Treff", cites)

    def test_ftp_delta_computation(self) -> None:
        """athlete_metrics 240→260 over the window ⇒ pct ≈ 8.3%."""
        plan = _mk_synth_plan(date.today() - timedelta(days=60), weeks=8)
        self._write_plan(plan)
        sd = plan["weeks"][0]["start"]
        ed = plan["weeks"][-1]["end"]

        # Synth FTP ledger: 240 at start, 260 at end.
        history = [
            {"date": sd, "value": 240.0, "source": "settings", "notes": None},
            {"date": ed, "value": 260.0, "source": "settings", "notes": None},
        ]
        with patch("db.query_metric_history",
                   side_effect=lambda metric, days=400: (
                       history if metric == "ftp" else [])):
            r = self.client.get("/api/programme/summary",
                                params={"plan_id": "test"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        ftp = d["ftp_delta"]
        self.assertEqual(ftp["start"], 240)
        self.assertEqual(ftp["end"], 260)
        # 20/240 = 8.333% — rounds to 8.3.
        self.assertAlmostEqual(ftp["pct"], 8.3, places=1)

    def test_intensity_distribution_sums_to_total(self) -> None:
        """Synth rides with zone-time totals propagate into intensity_dist."""
        plan = _mk_synth_plan(date.today() - timedelta(days=20), weeks=2)
        self._write_plan(plan)
        sd_str = plan["weeks"][0]["start"]

        # Two rides inside the window, one with each zone profile.
        synth_rides = [
            {
                "id": "r1",
                "started_at": f"{sd_str}T08:00:00",
                "finished_at": f"{sd_str}T10:00:00",
                "summary": {"tss": 80, "duration_sec": 7200,
                            "distance_km": 60.0},
                # 5 minutes Z1 + Z2, 1 minute Z3, 30 sec Z4 (in seconds)
                "zones": {"z1": 150, "z2": 150, "z3": 60, "z4": 30,
                          "z5": 0, "z6": 0, "z7": 0},
            },
            {
                "id": "r2",
                "started_at": f"{sd_str}T08:00:00",
                "finished_at": f"{sd_str}T10:00:00",
                "summary": {"tss": 60, "duration_sec": 3600,
                            "distance_km": 40.0},
                "zones": {"z1": 150, "z2": 150, "z3": 0, "z4": 0,
                          "z5": 0, "z6": 0, "z7": 0},
            },
        ]
        with patch("ride_storage.list_rides", return_value=synth_rides):
            r = self.client.get("/api/programme/summary",
                                params={"plan_id": "test"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        idist = d["intensity_dist"]
        # Z1+Z2: 150+150 + 150+150 = 600 sec = 10 min
        self.assertEqual(idist["z1z2_min"], 10)
        # Z3: 60 sec = 1 min
        self.assertEqual(idist["z3_min"], 1)
        # Z4+: 30 sec ≈ 0 or 1 min after rounding (round-half-to-even
        # → 0). Accept either.
        self.assertIn(idist["z4plus_min"], (0, 1))

    def test_compliance_per_phase(self) -> None:
        """One compliance entry per phase, with planned + actual TSS."""
        plan = _mk_synth_plan(date.today() - timedelta(days=28), weeks=4)
        self._write_plan(plan)
        sd_d = date.fromisoformat(plan["weeks"][0]["start"])

        # Synth one ride in each phase
        synth_rides = []
        for phase_idx in (0, 2):  # base ride day-2, build1 ride day-16
            d = sd_d + timedelta(days=phase_idx * 7 + 2)
            synth_rides.append({
                "id": f"r{phase_idx}",
                "started_at": f"{d.isoformat()}T08:00:00",
                "finished_at": f"{d.isoformat()}T10:00:00",
                "summary": {"tss": 200, "duration_sec": 7200,
                            "distance_km": 50.0},
                "zones": {"z1": 100, "z2": 100, "z3": 0, "z4": 0,
                          "z5": 0, "z6": 0, "z7": 0},
            })

        with patch("ride_storage.list_rides", return_value=synth_rides):
            r = self.client.get("/api/programme/summary",
                                params={"plan_id": "test"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        comp = d["compliance"]
        # Two phases in the synth plan → at least 2 entries.
        self.assertGreaterEqual(len(comp), 2)
        names = {e["phase"] for e in comp}
        self.assertIn("base", names)
        self.assertIn("build1", names)
        for entry in comp:
            self.assertIn("planned_tss", entry)
            self.assertIn("actual_tss", entry)
            self.assertIn("pct", entry)
            self.assertIsInstance(entry["pct"], int)


class ProgrammeSummaryPNGTests(unittest.TestCase):
    """Pillow-based PNG renderer — bytes start with the PNG magic header."""

    def test_png_render_returns_image_bytes(self) -> None:
        """render_programme_summary_png(synth) → starts with PNG magic."""
        from programme_summary_png import render_programme_summary_png
        synth_summary = {
            "plan_id": "test",
            "start_date": "2026-01-01",
            "end_date": "2026-04-01",
            "weeks": 12,
            "ftp_delta": {"start": 240, "end": 260, "pct": 8.3},
            "eftp_delta": {"start": 230, "end": 250, "pct": 8.7},
            "vo2max_delta": {"start": 52.0, "end": 56.0, "pct": 7.7},
            "ctl_gain": {"start": 36.0, "end": 64.0, "delta": 28.0},
            "intensity_dist": {"z1z2_min": 800, "z3_min": 60, "z4plus_min": 90},
            "pol_index": {"mean": 2.3, "class": "polarized"},
            "monotony_max": 1.8,
            "strain_max": 720.5,
            "compliance": [
                {"phase": "base", "planned_tss": 1200, "actual_tss": 1100, "pct": 92},
                {"phase": "build1", "planned_tss": 1600, "actual_tss": 1700, "pct": 106},
                {"phase": "build2", "planned_tss": 1800, "actual_tss": 1500, "pct": 83},
            ],
            "mean_max_curve": {
                "start": [{"dur": 5, "watts": 800}, {"dur": 60, "watts": 450},
                          {"dur": 300, "watts": 320},
                          {"dur": 1200, "watts": 260},
                          {"dur": 3600, "watts": 220}],
                "end": [{"dur": 5, "watts": 850}, {"dur": 60, "watts": 480},
                        {"dur": 300, "watts": 340},
                        {"dur": 1200, "watts": 280},
                        {"dur": 3600, "watts": 240}],
            },
            "hooper_trend": [{"week": i + 1, "mean": 12.0 + i * 0.3}
                             for i in range(12)],
            "totals": {"km": 2400, "hours": 96.5, "kj": 86000, "elev_m": 18000},
            "decoupling_trend": [{"week": i + 1, "mean_pct": 4.0 - i * 0.1}
                                 for i in range(12)],
            "citations": ["Stöggl 2014", "Foster 1998", "Treff 2019",
                          "Hooper 1995", "Coggan/Allen TR&P"],
        }
        png = render_programme_summary_png(synth_summary)
        self.assertIsInstance(png, bytes)
        # PNG magic: 89 50 4E 47 0D 0A
        self.assertTrue(png.startswith(b"\x89PNG\r\n"),
                        f"PNG header missing: first bytes = {png[:8]!r}")
        # Non-trivial size (>1KB).
        self.assertGreater(len(png), 1024)


if __name__ == "__main__":
    unittest.main()
