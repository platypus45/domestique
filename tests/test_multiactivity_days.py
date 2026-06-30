"""v2.4.0 — multi-activity days: a day with 2+ cycling rides must keep ALL of
them (so the planner shows all + sums their TSS), while non-cycling activities
are excluded from the cycling reconciliation. Pins the dedup fix (key now
includes duration) + the cycling-sport filter in _collect_week_activities.
"""
from __future__ import annotations

import datetime
import types
import unittest

import app
import db
import ride_storage


class TestMultiActivityCollection(unittest.TestCase):
    def setUp(self):
        self._db = db.query_activities
        self._rs = ride_storage.list_rides
        ride_storage.list_rides = lambda *a, **k: []  # isolate to db feed

    def tearDown(self):
        db.query_activities = self._db
        ride_storage.list_rides = self._rs

    def _collect(self, acts):
        db.query_activities = lambda *a, **k: acts
        cw = types.SimpleNamespace(start=datetime.date(2026, 6, 1))
        return app._collect_week_activities(cw, datetime.date(2026, 6, 3), include_today=True)

    def test_two_bikes_same_day_diff_duration_both_kept(self):
        """Commute + main ride with near-equal TSS but different duration must
        NOT collapse (pre-fix the (date, tss//5) key dropped the 2nd)."""
        out = self._collect([
            {"date": "2026-06-02", "sport": "Ride", "tss": 150, "duration_min": 40, "id": "commute"},
            {"date": "2026-06-02", "sport": "VirtualRide", "tss": 154, "duration_min": 70, "id": "main"},
        ])
        self.assertEqual(len(out), 2)
        self.assertEqual({o["id"] for o in out}, {"commute", "main"})

    def test_non_cycling_excluded(self):
        out = self._collect([
            {"date": "2026-06-02", "sport": "Run", "tss": 80, "duration_min": 40, "id": "run"},
            {"date": "2026-06-02", "sport": "Ride", "tss": 150, "duration_min": 60, "id": "bike"},
        ])
        self.assertEqual([o["id"] for o in out], ["bike"])

    def test_true_duplicate_still_deduped(self):
        """Same ride from two sources (same date+tss+duration buckets) still
        collapses to one — we didn't break cross-source dedup."""
        out = self._collect([
            {"date": "2026-06-02", "sport": "Ride", "tss": 150, "duration_min": 60, "id": "icu"},
            {"date": "2026-06-02", "sport": "Ride", "tss": 151, "duration_min": 61, "id": "fit"},
        ])
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
