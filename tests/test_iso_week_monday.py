"""v4.4.0 IMPL-SERVER §6 — ISO-week Monday-anchored boundary tests.

Three edge cases that previously broke when the server (or any helper that
recomputed week boundaries) accidentally used a Sunday-anchored grid:

  1. Sunday — last day of the ISO week. Belongs to that week's Monday-Sunday
     span, NOT to the next week's Monday.
  2. Monday — first day of the ISO week. The Monday helper returns the
     same date.
  3. Wednesday (midweek). Trivial sanity check.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta


def _monday_of(d: date) -> date:
    """The canonical Monday-anchored ISO-week start helper.

    Python's ``date.weekday()`` returns 0=Mon..6=Sun, so subtracting it from
    today always yields the Monday of the same ISO week. Tests pin the
    expected value rather than re-deriving it from isocalendar() to avoid
    the well-known Dec 28-31 ISO-year edge cases.
    """
    return d - timedelta(days=d.weekday())


class TestISOWeekMonday(unittest.TestCase):
    def test_sunday_edge_belongs_to_prior_monday(self):
        # Sun 2026-04-26 — last day of ISO week 17, Monday is 2026-04-20.
        sun = date(2026, 4, 26)
        self.assertEqual(sun.weekday(), 6, "expected Sunday=6")
        self.assertEqual(_monday_of(sun), date(2026, 4, 20))

    def test_monday_edge_returns_same_day(self):
        mon = date(2026, 4, 20)
        self.assertEqual(mon.weekday(), 0, "expected Monday=0")
        self.assertEqual(_monday_of(mon), mon)

    def test_wednesday_midweek(self):
        wed = date(2026, 4, 22)
        self.assertEqual(wed.weekday(), 2, "expected Wednesday=2")
        self.assertEqual(_monday_of(wed), date(2026, 4, 20))

    def test_calendar_endpoint_uses_monday_anchor(self):
        """Spot-check: /api/calendar emits weeks with start_date on a Monday
        and dow=1..7 mapping to Mon..Sun.

        This is the real production helper exercise — guards against any
        future regression that swaps in a Sunday-anchored grid.
        """
        import app as app_module
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        # We don't care if there's a plan or rides; only the ISO mapping.
        r = client.get("/api/calendar")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        for w in data.get("weeks", []):
            sd = w.get("start_date")
            if not sd:
                continue
            d = date.fromisoformat(sd)
            self.assertEqual(d.weekday(), 0,
                             f"week start {sd} not Monday (weekday={d.weekday()})")
            # First day in the week's days list is the same Monday with dow=1.
            days = w.get("days", [])
            if days:
                self.assertEqual(days[0]["dow"], 1)
                self.assertEqual(date.fromisoformat(days[0]["date"]), d)
                # Sunday is dow=7.
                self.assertEqual(days[-1]["dow"], 7)
                self.assertEqual(
                    date.fromisoformat(days[-1]["date"]),
                    d + timedelta(days=6),
                )


if __name__ == "__main__":
    unittest.main()
