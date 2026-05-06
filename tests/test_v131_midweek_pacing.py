"""v1.3.1 HIGH hot-fix — time-aware mid-week pacing math.

The bug: ``/api/calendar`` summed ``planned_*`` across all 7 days, so on
Wednesday the user's "compliance" was their actual TSS divided by the
FULL-week plan — making them look "behind plan" even when Mon/Tue/Wed
were complete.

The fix: emit ``planned_*_to_date`` alongside the full-week totals;
front-end grades headline % against to-date.

These tests pin the server contract:
    1. Wednesday — to_date_target = sum(Mon..Wed planned).
    2. Sunday    — to_date_target = full-week planned.
    3. Monday    — to_date_target = Mon planned only.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import patch

import app as app_module


def _mk_plan(monday: date) -> dict:
    """Mon-Sun plan with mixed durations so to-date sums are non-trivial.

    Each row gives (duration_min, tss, [zone_dist percentages summing to 100]).

    Mon  60 min  100% Z1+Z2          tss 50  → z12 60
    Tue  75 min  60% Z1+Z2 / 40% Z3+Z4  tss 75  → z12 45, z34 30
    Wed  90 min  100% Z1+Z2          tss 70  → z12 90
    Thu  REST                         tss  0
    Fri 120 min  100% Z1+Z2          tss 100 → z12 120
    Sat 150 min  87% Z1+Z2 / 13% Z5+ tss 130 → z12 130.5, z5 19.5 (≈20)
    Sun  REST                         tss  0
    Full-week z12 ≈ 445.5, z34 = 30, z5 ≈ 19.5, tss = 425
    """
    sessions = []
    # zone_dist values are integer-ish percentages (sum=100) — _planned_zone_split_minutes
    # multiplies through duration to get minutes.
    rows = [
        # off, dur, tss, z1, z2, z3, z4, z5, z6, label
        (0,  60,  50,   0, 100, 0,  0,  0, 0, "z2 60"),
        (1,  75,  75,   0,  60, 0, 40,  0, 0, "tempo 75"),
        (2,  90,  70,   0, 100, 0,  0,  0, 0, "z2 90"),
        (3,   0,   0,   0,   0, 0,  0,  0, 0, "rest"),
        (4, 120, 100,   0, 100, 0,  0,  0, 0, "z2 120"),
        (5, 150, 130,   0,  87, 0,  0, 13, 0, "vo2 150"),
        (6,   0,   0,   0,   0, 0,  0,  0, 0, "rest"),
    ]
    for off, dur, tss, z1, z2, z3, z4, z5, z6, label in rows:
        d = monday + timedelta(days=off)
        s = {
            "day": d.isoformat(),
            "day_name": d.strftime("%a"),
            "session_type": "rest" if dur == 0 else "z2",
            "duration_min": dur,
            "tss_estimate": tss,
            "description": label,
            "zwo_file": "" if dur == 0 else f"{label.replace(' ', '_')}.zwo",
            "zwo_name": label,
            "status": "pending",
        }
        if dur > 0:
            s["zone_dist"] = {"z1": z1, "z2": z2, "z3": z3, "z4": z4, "z5": z5, "z6": z6}
        sessions.append(s)
    return {
        "goal": {"type": "general", "hours_per_week": 8.0},
        "phases": [],
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "phase": "base",
            "tss_target": 425,
            "is_stepback": False,
            "sessions": sessions,
        }],
        "generated": "2026-04-19T00:00:00",
    }


def _z12_min_of_session(s: dict) -> float:
    """Mirror _planned_zone_split_minutes for the Z1+Z2 channel."""
    dur = float(s.get("duration_min") or 0)
    if dur <= 0:
        return 0.0
    zd = s.get("zone_dist") or {}
    pct = float(zd.get("z1") or 0) + float(zd.get("z2") or 0)
    total = sum(float(zd.get(k) or 0) for k in ("z1","z2","z3","z4","z5","z6"))
    if total <= 0:
        return 0.0
    return round(dur * pct / 100.0, 1)


def _full_week_z12(plan: dict) -> float:
    return round(sum(_z12_min_of_session(s) for s in plan["weeks"][0]["sessions"]), 1)


def _to_date_z12(plan: dict, today: date) -> float:
    iso = today.isoformat()
    return round(sum(
        _z12_min_of_session(s)
        for s in plan["weeks"][0]["sessions"]
        if s["day"] <= iso
    ), 1)


def _to_date_tss(plan: dict, today: date) -> float:
    iso = today.isoformat()
    return sum(
        float(s.get("tss_estimate") or 0)
        for s in plan["weeks"][0]["sessions"]
        if s["day"] <= iso
    )


class _PinnedDate(date):
    """Subclass of `date` whose .today() returns a fixed value.

    Used via patch.object(app, 'date', ...) so app.merge_plan_with_rides
    sees the pinned date without us having to monkeypatch every callsite.
    """
    _pinned: date = date(2026, 5, 6)  # default — overridden per test

    @classmethod
    def today(cls) -> "date":
        return cls._pinned


def _make_pinned(d: date):
    """Build a `date` subclass class with `today()` pinned to `d`."""
    class _P(date):
        @classmethod
        def today(cls) -> "date":
            return d
    return _P


class MidWeekPacingTests(unittest.TestCase):

    # Use a fixed Monday well in the past so ISO-week math is stable.
    MONDAY = date(2026, 5, 4)  # Monday 2026-W19

    def _run(self, today: date) -> dict:
        plan = _mk_plan(self.MONDAY)
        Pinned = _make_pinned(today)
        with patch.object(app_module, "date", Pinned):
            payload = app_module.merge_plan_with_rides(plan, rides=[])
        return payload, plan

    # ── Test 1: Wednesday ────────────────────────────────────────────────
    def test_wednesday_to_date_target_is_mon_tue_wed(self):
        wednesday = self.MONDAY + timedelta(days=2)
        payload, plan = self._run(wednesday)
        cur = next(w for w in payload["weeks"] if w["is_current"])

        expected_z12_td = _to_date_z12(plan, wednesday)         # 60+45+90 = 195
        expected_tss_td = _to_date_tss(plan, wednesday)         # 50+75+70 = 195
        full_z12 = _full_week_z12(plan)                         # 475

        # The to-date plan rolls up only Mon..Wed.
        self.assertAlmostEqual(cur["planned_z1z2_min_to_date"], expected_z12_td, places=1)
        self.assertAlmostEqual(cur["planned_tss_to_date"], expected_tss_td, places=1)
        # Full-week plan unchanged.
        self.assertAlmostEqual(cur["planned_z1z2_min"], full_z12, places=1)
        # Annotation: 3 of 7 days elapsed.
        self.assertEqual(cur["days_elapsed"], 3)
        self.assertEqual(cur["days_total"], 7)

        # The screenshot's example: actual_z12 = 37, full-week plan = 360 → 10%.
        # With to-date math the percentage uses 195 as the denominator → 19%
        # (still amber/red, but no longer the misleading 10%). Pin the
        # contract: percentage MUST be against to-date, not full week.
        # Simulate by setting a synthetic 37-min ride (fake).
        # We don't drive a ride here — just assert the field exists and
        # would be the denominator the front-end uses.
        self.assertGreater(
            cur["planned_z1z2_min_to_date"], 0,
            "to_date_target must be > 0 mid-week"
        )
        self.assertLess(
            cur["planned_z1z2_min_to_date"], cur["planned_z1z2_min"],
            "Wed to_date_target must be smaller than full-week target"
        )

    # ── Test 2: Sunday end-of-week ───────────────────────────────────────
    def test_sunday_to_date_equals_full_week(self):
        sunday = self.MONDAY + timedelta(days=6)
        payload, plan = self._run(sunday)
        cur = next(w for w in payload["weeks"] if w["is_current"])

        # On the last day, the to-date target == full-week target so the
        # math collapses back to the v1.3.0 behavior.
        self.assertAlmostEqual(
            cur["planned_z1z2_min_to_date"], cur["planned_z1z2_min"], places=1
        )
        self.assertAlmostEqual(
            cur["planned_tss_to_date"], cur["planned_tss"], places=1
        )
        self.assertEqual(cur["days_elapsed"], 7)

    # ── Test 3: Monday at week start ─────────────────────────────────────
    def test_monday_to_date_is_monday_only(self):
        monday = self.MONDAY
        payload, plan = self._run(monday)
        cur = next(w for w in payload["weeks"] if w["is_current"])

        # Monday's planned-only: z12=60, tss=50.
        self.assertAlmostEqual(cur["planned_z1z2_min_to_date"], 60.0, places=1)
        self.assertAlmostEqual(cur["planned_tss_to_date"], 50.0, places=1)
        self.assertEqual(cur["days_elapsed"], 1)
        # And the full-week target is bigger.
        self.assertGreater(
            cur["planned_z1z2_min"], cur["planned_z1z2_min_to_date"]
        )


if __name__ == "__main__":
    unittest.main()
