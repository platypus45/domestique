"""v2.3 — F7 B/C races. F7a: the events[] data model + persistence round-trip.

The A event stays the canonical Goal.target_date + event_* scalars (single-A plans
unchanged). Optional B/C events ride in Goal.events (TargetEvent list) and must
survive the saved-plan round-trip (save -> _goal_from_plan_dict) so a recalc keeps
them. Empty events = today's single-A behaviour.
"""
from datetime import date, timedelta
import unittest

import app
import training_planner as tp


class TestEventsRoundTrip(unittest.TestCase):
    def test_no_events_is_empty(self):
        # single-A back-compat: a goal block without events[] → no B/C events.
        goal = app._goal_from_plan_dict({"type": "event"})
        self.assertEqual(goal.events, [])

    def test_events_roundtrip_through_goal_block(self):
        td = (date.today() + timedelta(weeks=10)).isoformat()
        bd = (date.today() + timedelta(weeks=5)).isoformat()
        g = {
            "type": "event", "event_date": td,
            "events": [
                {"date": td, "priority": "A", "name": "Goal GF", "event_km": 160},
                {"date": bd, "priority": "B", "name": "Tune-up", "event_km": 90,
                 "event_climb_m": 1200, "event_type": "granfondo"},
            ],
        }
        goal = app._goal_from_plan_dict(g)
        self.assertEqual(len(goal.events), 2)
        b = [e for e in goal.events if e.priority == "B"][0]
        self.assertEqual(b.date, date.today() + timedelta(weeks=5))
        self.assertEqual(b.event_km, 90)
        self.assertEqual(b.event_climb_m, 1200)
        # re-serialize → same count + priorities (save round-trip)
        back = app._events_to_dicts(goal.events)
        self.assertEqual({d["priority"] for d in back}, {"A", "B"})

    def test_from_dicts_skips_dateless_entries(self):
        evs = app._events_from_dicts([{"priority": "B"}, {"date": None}])
        self.assertEqual(evs, [])

    def test_targetevent_defaults(self):
        e = tp.TargetEvent(date=date.today())
        self.assertEqual(e.priority, "B")
        self.assertEqual(e.event_km, 0)


if __name__ == "__main__":
    unittest.main()
