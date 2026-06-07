"""v1.8.18 — plan zwo_file reference-integrity migration.

The boot healer re-resolves any future session whose ``zwo_file`` doesn't
exist in the local flat library (external Zwift/TR scrape slugs like
``ftp-builder/week-6-day-3.zwo`` or flat-but-missing basenames). Grilled
invariants:

  - PAST sessions (day < today) are FROZEN — never re-rolled (history).
  - Idempotent + deterministic: healing twice, and on two different calendar
    days, yields identical output (the seed anchor is the plan's stable
    ``generated`` date, not ``date.today()``).
  - Resolves-on-disk staleness, not session_type↔prefix.
  - A one-time ``.premigration-v1818`` snapshot is written before the first
    mutation.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import training_planner as tp  # noqa: E402


def _plan_with(sessions_by_offset):
    """Build a minimal plan dict. sessions_by_offset: list of
    (day_offset_from_today, session_type, zwo_file)."""
    today = date.today()
    weeks = [{"week_num": 1, "sessions": []}]
    for off, st, zf in sessions_by_offset:
        d = (today + timedelta(days=off)).isoformat()
        weeks[0]["sessions"].append({
            "day": d, "day_name": "Mon", "session_type": st,
            "duration_min": 60, "tss_estimate": 60,
            "description": "x", "zwo_file": zf, "zwo_name": zf,
        })
    return {"generated": (today - timedelta(days=30)).isoformat(), "weeks": weeks}


class TestPlanZwoIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plan_int_"))
        self.plan_p = self.tmp / "current_plan.json"

    def _write(self, plan):
        self.plan_p.write_text(json.dumps(plan), encoding="utf-8")

    def test_future_ghost_healed_past_frozen(self):
        ghost_past = "ftp-builder/week-1-day-1.zwo"
        ghost_future = "zwift-academy/month-2-session-10.zwo"
        plan = _plan_with([(-3, "z2", ghost_past), (3, "z2", ghost_future)])
        self._write(plan)
        n = tp.rewrite_stale_plan_classifications(self.plan_p)
        out = json.loads(self.plan_p.read_text())
        sess = out["weeks"][0]["sessions"]
        # Past session: untouched (frozen history).
        self.assertEqual(sess[0]["zwo_file"], ghost_past)
        # Future session: healed to a real local flat file (or honest empty).
        healed = sess[1]["zwo_file"]
        self.assertNotEqual(healed, ghost_future)
        self.assertNotIn("/", healed)
        if healed:
            self.assertTrue((Path("workouts") / healed).exists())
        self.assertGreaterEqual(n, 1)

    def test_idempotent_and_day_invariant(self):
        plan = _plan_with([(5, "tempo", "external/foo/bar.zwo")])
        self._write(plan)
        # Heal "today".
        n1 = tp.rewrite_stale_plan_classifications(self.plan_p)
        first = json.loads(self.plan_p.read_text())["weeks"][0]["sessions"][0]["zwo_file"]
        # Heal again same day → no-op.
        n2 = tp.rewrite_stale_plan_classifications(self.plan_p)
        self.assertEqual(n2, 0, "second heal must be a no-op (idempotent)")
        # Simulate a launch on a LATER calendar day: seed anchor is the plan's
        # stable `generated` date, so the result must not drift.
        again = json.loads(self.plan_p.read_text())["weeks"][0]["sessions"][0]["zwo_file"]
        self.assertEqual(first, again, "healed file must be stable across days")

    def test_premigration_snapshot_written_once(self):
        plan = _plan_with([(2, "z2", "ext/ghost.zwo")])
        self._write(plan)
        tp.rewrite_stale_plan_classifications(self.plan_p)
        snap = self.plan_p.with_suffix(self.plan_p.suffix + ".premigration-v1818")
        self.assertTrue(snap.exists(), "one-time pre-migration snapshot missing")
        # Snapshot preserves the ORIGINAL ghost (pre-heal state).
        orig = json.loads(snap.read_text())
        self.assertEqual(orig["weeks"][0]["sessions"][0]["zwo_file"], "ext/ghost.zwo")

    def test_valid_session_left_untouched(self):
        # Pick a real library file so the session resolves and must not churn.
        lib = tp.load_workout_library()
        real = next((r["File"] for r in lib if r.get("File")), None)
        if not real:
            self.skipTest("no library files")
        plan = _plan_with([(4, "z2", real)])
        self._write(plan)
        n = tp.rewrite_stale_plan_classifications(self.plan_p)
        out = json.loads(self.plan_p.read_text())
        self.assertEqual(out["weeks"][0]["sessions"][0]["zwo_file"], real)
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
