"""v1.3.4 — generate-plan paints yellow ⚠ on every cell when availability
is heavy enough to push session durations past library coverage.

User report: a freshly-generated 12-week plan painted yellow ⚠ on every
non-rest cell. Tooltip read e.g. "z2 (70min) — sampled from library · 154m"
— description embedded the original library duration but the session
target was 154min after availability rescale.

Three root causes:

1. ``match_zwo`` returned ``zwo_file=""`` when the requested duration
   exceeded library coverage (e.g. 222-min vo2max — library tops at
   150min). ``_classify_card_state`` flags every empty zwo as
   ``missing_workout`` → yellow. Fix: fall back to the longest available
   workout in the right category.
2. The injected mid-cycle ftp_test slot kept ``zwo_file=""`` because
   match_zwo's want_test branch had no candidates (Protocol-category gate
   filtered out every ftp_test-tagged ZWO). Fix: bypass the category gate
   when ``want_test`` is True; tag filter alone is sufficient.
3. ``description`` embedded the original ZWO's duration; after availability
   rescale + re-match, it stayed stale ("z2 (70min) ...") even though
   ``s.duration_min`` was 154. Fix: refresh description after re-match.

The headline test: a 12-week plan with realistic high-volume availability
(weekend 4h, weekday 1-1.5h) must produce <5% yellow cells.
"""
from __future__ import annotations

import json
import shutil
import unittest
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


def _client() -> TestClient:
    return TestClient(app_module.app)


def _seed_high_volume_availability(plan_path: Path) -> dict:
    """Persist a 12-week realistic-heavy availability calendar to plan.json."""
    with plan_path.open() as f:
        plan = json.load(f)
    weekday_hours = {0: 1.5, 1: 1.5, 2: 1.0, 3: 1.5, 4: 0.5, 5: 4.0, 6: 4.0}
    today = date.today()
    new_avail: dict = {}
    for i in range(7 * 12):
        d = today + timedelta(days=i)
        h = weekday_hours.get(d.weekday(), 1.0)
        new_avail[d.isoformat()] = {
            "hours": h, "type": "available" if h > 0 else "unavailable"
        }
    plan["availability"] = new_avail
    with plan_path.open("w") as f:
        json.dump(plan, f, indent=2, default=str)
    return plan


class TestV134NoYellowOnGenerate(unittest.TestCase):
    """Reproduces the user-visible v1.3.4 bug + verifies the fix."""

    @classmethod
    def setUpClass(cls):
        tp.load_workout_library()
        cls.plan_path = tp.PLAN_DIR / "current_plan.json"
        if cls.plan_path.exists():
            cls.backup = cls.plan_path.with_suffix(".json.v134_test_bak")
            shutil.copy(cls.plan_path, cls.backup)
        else:
            cls.backup = None

    @classmethod
    def tearDownClass(cls):
        if cls.backup and cls.backup.exists():
            shutil.copy(cls.backup, cls.plan_path)
            cls.backup.unlink()

    def test_high_volume_availability_under_5pct_yellow(self):
        """User repro: realistic heavy availability → <5% missing_workout."""
        if not self.plan_path.exists():
            self.skipTest("no current plan to seed availability into")
        _seed_high_volume_availability(self.plan_path)

        today = date.today()
        with _client() as client:
            r = client.post("/api/plan/generate", json={
                "goal": "event", "plan_weeks": 12, "hours_per_week": 14,
                "event_date": (today + timedelta(weeks=12)).isoformat(),
                "event_name": "v134-test", "event_km": 200,
                "event_climb": 1500, "event_type": "gran fondo",
            })
        self.assertEqual(r.status_code, 200, r.text[:300])
        plan = r.json()["plan_json"]

        counts: dict[str, int] = {}
        for w in plan.get("weeks", []) or []:
            for s in w.get("sessions", []) or []:
                cs = s.get("card_state") or "?"
                counts[cs] = counts.get(cs, 0) + 1
        total_non_rest = counts.get("planned", 0) + counts.get("missing_workout", 0)
        self.assertGreater(total_non_rest, 0)
        ratio = counts.get("missing_workout", 0) / total_non_rest
        self.assertLess(
            ratio, 0.05,
            f"yellow ⚠ ratio {ratio:.1%} (>5%). counts={counts}",
        )

    def test_long_duration_z2_falls_back_to_longest(self):
        """A 240-min long_z2 slot should pick the longest endurance file
        instead of zwo_file=''.
        """
        from training_planner import (
            PlannedSession, load_workout_library, match_zwo,
        )
        lib = load_workout_library()
        s = PlannedSession(
            day=date.today(),
            day_name="Sat",
            session_type="long_z2",
            duration_min=240,
            tss_estimate=180,
            description="long_z2 (240min) — sampled from library",
        )
        match_zwo(s, lib, plan_start_date=date.today())
        self.assertTrue(
            getattr(s, "zwo_file", "") or "",
            "240-min long_z2 should fall back to longest Endurance file"
            " not ''",
        )

    def test_ftp_test_session_picks_a_test_zwo(self):
        """ftp_test session (Protocol mismatch was eating all candidates)
        must now find a Coggan-20 / Ramp ZWO from the library.
        """
        from training_planner import (
            PlannedSession, load_workout_library, match_zwo,
        )
        lib = load_workout_library()
        s = PlannedSession(
            day=date.today(),
            day_name="Tue",
            session_type="ftp_test",
            duration_min=60,
            tss_estimate=70,
            description="FTP TEST — Coggan-20 or Ramp protocol",
        )
        match_zwo(s, lib, plan_start_date=date.today())
        self.assertTrue(
            getattr(s, "zwo_file", "") or "",
            "ftp_test session should pick a tagged ftp_test ZWO, not ''",
        )


if __name__ == "__main__":
    unittest.main()
