"""v1.0.3 IMPL-WIRING — GET /api/plan/missed-suggestions contract tests.

Five tests cover the read-only suggestions endpoint:

  1. test_no_misses_returns_empty       — clean plan → ``{"suggestions":[]}``
  2. test_single_miss_with_rest_slot    — one miss + one rest slot in same week
                                          → one suggestion, ``reason="rest_slot"``
  3. test_two_misses_greedy_first_fit   — two misses race for one slot →
                                          first-by-date wins, second skips
  4. test_miss_with_only_past_slots     — slots in the same week but all past
                                          → no suggestion emitted
  5. test_unavailable_day_excluded      — ``availability[D].type == "unavailable"``
                                          excludes that slot from suggestions

Locked endpoint shape (per MASTER §1):
    {
      "suggestions": [
        {
          "missed_date": "YYYY-MM-DD",
          "missed_session_type": "...",
          "missed_summary": "...",
          "suggested_date": "YYYY-MM-DD",
          "suggested_day_name": "Fri",
          "reason": "rest_slot" | "unfilled_available_day",
        },
        ...
      ]
    }

The endpoint applies the locked six-rule "available slot" predicate
(MASTER §1 — `available slot` rule) and emits at most one suggestion
per missed session via greedy first-fit by ``missed_date`` ascending.
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


def _mk_session(d: date, *, session_type: str = "rest",
                duration_min: int = 0, tss: float = 0.0,
                status: str = "pending",
                description: str = "",
                user_moved: bool = False,
                dismissed_at: str = "") -> dict:
    return {
        "day": d.isoformat(),
        "day_name": d.strftime("%a"),
        "session_type": session_type,
        "duration_min": duration_min,
        "tss_estimate": tss,
        "description": description,
        "zwo_file": "",
        "zwo_name": "",
        "status": status,
        "user_moved": user_moved,
        "dismissed_at": dismissed_at,
    }


def _build_plan(monday: date, sessions: list[dict], *,
                rest_days: list[int] | None = None,
                available_days: list[int] | None = None,
                availability: dict | None = None) -> dict:
    """Build a minimal one-week plan covering Mon..Sun.

    ``sessions`` is a 7-element list (one per weekday).
    """
    if rest_days is None:
        rest_days = [6]  # Sun rest
    if available_days is None:
        available_days = [0, 1, 2, 3, 4, 5]
    plan = {
        "goal": {
            "type": "general",
            "hours_per_week": 8.0,
            "rest_days": rest_days,
            "available_days": available_days,
        },
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "phase": "base",
            "tss_target": 270.0,
            "is_stepback": False,
            "sessions": sessions,
            "hit_per_week": 1,
        }],
        "availability": availability or {},
        "generated": "2026-04-19T00:00:00",
    }
    return plan


class MissedSuggestionsBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)

        # Pick a Monday far enough in the future that all 7 weekdays
        # satisfy rule §2.1 (D >= today). We'll override "today" inside
        # individual tests where past-week scenarios matter.
        today = date.today()
        days_until_mon = (7 - today.weekday()) % 7
        if days_until_mon < 2:
            days_until_mon += 7
        self._monday = today + timedelta(days=days_until_mon)

        self._json_path = self._tmp / "current_plan.json"

        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp

        self.client = TestClient(app_module.app)

    def tearDown(self):
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def _write(self, plan: dict) -> None:
        self._json_path.write_text(json.dumps(plan))


class TestNoMissesReturnsEmpty(MissedSuggestionsBase):
    def test_no_misses_returns_empty(self):
        sessions = [
            _mk_session(self._monday + timedelta(days=i),
                        session_type="z2" if i in (0, 2) else "rest",
                        status="pending")
            for i in range(7)
        ]
        self._write(_build_plan(self._monday, sessions))

        resp = self.client.get("/api/plan/missed-suggestions")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body, {"suggestions": []})


class TestSingleMissWithRestSlot(MissedSuggestionsBase):
    """Mon=missed Z2, Wed=rest → expect one suggestion, reason=rest_slot."""

    def test_single_miss_with_rest_slot(self):
        sessions = []
        for i in range(7):
            d = self._monday + timedelta(days=i)
            if i == 0:
                # Monday: missed endurance ride.
                sessions.append(_mk_session(
                    d, session_type="endurance", duration_min=60, tss=45.0,
                    status="missed", description="Z2 endurance, 60min",
                ))
            elif i == 2:
                # Wednesday: rest slot, available.
                sessions.append(_mk_session(d, session_type="rest", status="pending"))
            else:
                sessions.append(_mk_session(d, session_type="z2", duration_min=60,
                                            tss=45.0, status="pending"))
        self._write(_build_plan(self._monday, sessions))

        resp = self.client.get("/api/plan/missed-suggestions")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["suggestions"]), 1)
        s = body["suggestions"][0]
        self.assertEqual(s["missed_date"], self._monday.isoformat())
        self.assertEqual(s["missed_session_type"], "endurance")
        self.assertEqual(s["suggested_date"],
                         (self._monday + timedelta(days=2)).isoformat())
        self.assertEqual(s["reason"], "rest_slot")
        self.assertEqual(s["suggested_day_name"],
                         (self._monday + timedelta(days=2)).strftime("%a"))


class TestTwoMissesGreedyFirstFit(MissedSuggestionsBase):
    """Two misses (Mon, Tue) + one rest slot (Wed) → only the earlier miss gets it."""

    def test_two_misses_greedy_first_fit(self):
        sessions = []
        for i in range(7):
            d = self._monday + timedelta(days=i)
            if i == 0:
                sessions.append(_mk_session(d, session_type="endurance",
                                            duration_min=60, tss=45.0,
                                            status="missed"))
            elif i == 1:
                sessions.append(_mk_session(d, session_type="threshold",
                                            duration_min=75, tss=90.0,
                                            status="missed"))
            elif i == 2:
                # The only rest slot.
                sessions.append(_mk_session(d, session_type="rest",
                                            status="pending"))
            else:
                sessions.append(_mk_session(d, session_type="z2",
                                            duration_min=60, tss=45.0,
                                            status="pending"))
        self._write(_build_plan(self._monday, sessions))

        resp = self.client.get("/api/plan/missed-suggestions")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Greedy first-fit: only Monday's miss gets the Wednesday slot;
        # Tuesday's miss is skipped (no remaining slot).
        self.assertEqual(len(body["suggestions"]), 1)
        s = body["suggestions"][0]
        self.assertEqual(s["missed_date"], self._monday.isoformat())
        self.assertEqual(s["suggested_date"],
                         (self._monday + timedelta(days=2)).isoformat())


class TestMissWithOnlyPastSlots(MissedSuggestionsBase):
    """Past-week miss with no slots in the future → no suggestion."""

    def test_miss_with_only_past_slots(self):
        # Use last week so every D in the week is in the past relative to today.
        today = date.today()
        last_monday = today - timedelta(days=today.weekday() + 7)
        sessions = []
        for i in range(7):
            d = last_monday + timedelta(days=i)
            if i == 0:
                sessions.append(_mk_session(d, session_type="endurance",
                                            duration_min=60, tss=45.0,
                                            status="missed"))
            elif i == 2:
                sessions.append(_mk_session(d, session_type="rest",
                                            status="pending"))
            else:
                sessions.append(_mk_session(d, session_type="z2",
                                            duration_min=60, tss=45.0,
                                            status="pending"))
        self._write(_build_plan(last_monday, sessions))

        resp = self.client.get("/api/plan/missed-suggestions")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # All slots are in the past → rule §2.1 excludes everything.
        self.assertEqual(body["suggestions"], [])


class TestUnavailableDayExcluded(MissedSuggestionsBase):
    """availability[Wed].type == 'unavailable' → that slot is excluded."""

    def test_unavailable_day_excluded(self):
        sessions = []
        for i in range(7):
            d = self._monday + timedelta(days=i)
            if i == 0:
                sessions.append(_mk_session(d, session_type="endurance",
                                            duration_min=60, tss=45.0,
                                            status="missed"))
            elif i == 2:
                # Wed: a rest slot, but the user marked it unavailable.
                sessions.append(_mk_session(d, session_type="rest",
                                            status="pending"))
            elif i == 4:
                # Fri: a fallback rest slot, available.
                sessions.append(_mk_session(d, session_type="rest",
                                            status="pending"))
            else:
                sessions.append(_mk_session(d, session_type="z2",
                                            duration_min=60, tss=45.0,
                                            status="pending"))
        wed_iso = (self._monday + timedelta(days=2)).isoformat()
        plan = _build_plan(self._monday, sessions, availability={
            wed_iso: {"hours": 0, "type": "unavailable"},
        })
        self._write(plan)

        resp = self.client.get("/api/plan/missed-suggestions")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["suggestions"]), 1)
        s = body["suggestions"][0]
        # Must NOT pick Wed (excluded) → must pick Fri (next available).
        self.assertEqual(s["suggested_date"],
                         (self._monday + timedelta(days=4)).isoformat())
        self.assertEqual(s["reason"], "rest_slot")


if __name__ == "__main__":
    unittest.main()
