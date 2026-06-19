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

    def test_ui_post_shape_roundtrips(self):
        # F7c: the exact events[] the plan form POSTs — the A event (with climb)
        # plus a B/C row from readBcRaces() (date + priority + name + event_km,
        # no climb, no type) — round-trips into valid TargetEvents with sane
        # defaults (climb→0, type→granfondo).
        td = (date.today() + timedelta(weeks=12)).isoformat()
        cd = (date.today() + timedelta(weeks=4)).isoformat()
        evs = app._events_from_dicts([
            {"date": td, "priority": "A", "name": "Goal GF",
             "event_km": 160, "event_climb_m": 3000, "event_type": "granfondo"},
            {"date": cd, "priority": "C", "name": "Local crit", "event_km": 40},
        ])
        self.assertEqual([e.priority for e in evs], ["A", "C"])
        c = evs[1]
        self.assertEqual(c.event_km, 40)
        self.assertEqual(c.event_climb_m, 0)        # B/C row omits climb → 0
        self.assertEqual(c.event_type, "granfondo")  # omitted → default


class TestSecondaryEventTapers(unittest.TestCase):
    """F7b: a B/C event gets a short mini-taper (no HIT in its window); a single-A
    plan is unchanged (the mini-taper is a no-op without B/C events)."""

    def _goal(self, events=None):
        A = date.today() + timedelta(weeks=12)
        kw = dict(goal_type="event", plan_weeks=12, target_date=A,
                  event_km=160, event_climb_m=2000, event_type="gran_fondo",
                  hours_per_week=12.0, max_weekday_hours=2.5, max_weekend_hours=4.0,
                  available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[])
        if events is not None:
            kw["events"] = events
        return tp.Goal(**kw)

    def test_b_event_window_has_no_hit(self):
        A = date.today() + timedelta(weeks=12)
        B = date.today() + timedelta(weeks=6)  # mid-plan build, well before the A taper
        goal = self._goal(events=[
            tp.TargetEvent(date=A, priority="A"),
            tp.TargetEvent(date=B, priority="B"),
        ])
        _ph, weeks = tp.generate_plan(goal, athlete={"ftp": 250, "weight_kg": 70},
                                      recent_weekly_tss=500)
        for w in weeks:
            for s in w.sessions:
                d = getattr(s, "day", None)
                if d and s.session_type != "rest" and 0 <= (B - d).days <= 2:
                    self.assertFalse(tp._session_is_hit(s),
                                     f"HIT inside the B-race mini-taper window: {d}")

    def test_single_a_plan_keeps_its_hit(self):
        # No events[] → _apply_secondary_event_tapers is a no-op → a normal plan
        # (still has hard sessions; the mini-taper didn't strip the build).
        _ph, weeks = tp.generate_plan(self._goal(), athlete={"ftp": 250, "weight_kg": 70},
                                      recent_weekly_tss=500)
        self.assertTrue(any(tp._session_is_hit(s) for w in weeks for s in w.sessions),
                        "single-A plan should still contain HIT sessions")


if __name__ == "__main__":
    unittest.main()
