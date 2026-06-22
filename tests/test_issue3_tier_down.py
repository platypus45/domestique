"""Issue #3 — the readiness tier-down must never INCREASE load, and a severe day
drops all the way to easy in one pass (R1).

Before: apply_week_tier_down kept the duration and applied the new type's TSS/h, so
vo2max(75/h) → threshold(90/h) RAISED TSS at the same duration (rider saw 64→76.5).
And "auto-adjust" only ever dropped one ladder step.
"""
import unittest
from datetime import date, timedelta

import training_planner as tp

_HARD = {"vo2max", "threshold", "overunder", "sweetspot", "sprint", "tempo"}


def _plan():
    mon = date.today() - timedelta(days=date.today().weekday())
    return {"weeks": [{"week_num": 1, "start": mon.isoformat(), "sessions": [
        {"day": (mon + timedelta(days=2)).isoformat(), "day_name": "Wed",
         "session_type": "vo2max", "duration_min": 51, "tss_estimate": 64.0, "status": "pending"},
        {"day": (mon + timedelta(days=4)).isoformat(), "day_name": "Fri",
         "session_type": "threshold", "duration_min": 80, "tss_estimate": 120.0, "status": "pending"},
    ]}]}, mon


class TestTierDown(unittest.TestCase):
    def test_one_tier_never_increases_tss(self):
        plan, mon = _plan()
        orig = {s["day_name"]: s["tss_estimate"] for s in plan["weeks"][0]["sessions"]}
        tp.apply_week_tier_down(plan, mon.isoformat(), to_floor=False)
        for s in plan["weeks"][0]["sessions"]:
            self.assertLessEqual(
                s["tss_estimate"], orig[s["day_name"]] + 0.5,
                f"tier-down raised TSS for {s['day_name']}: {orig[s['day_name']]} -> {s['tss_estimate']}")

    def test_to_floor_drops_all_the_way_to_easy(self):
        plan, mon = _plan()
        orig = {s["day_name"]: s["tss_estimate"] for s in plan["weeks"][0]["sessions"]}
        tp.apply_week_tier_down(plan, mon.isoformat(), to_floor=True)
        for s in plan["weeks"][0]["sessions"]:
            self.assertNotIn(s["session_type"], _HARD,
                             f"{s['day_name']} still hard ({s['session_type']}) after to_floor")
            self.assertLessEqual(s["tss_estimate"], orig[s["day_name"]] + 0.5)


if __name__ == "__main__":
    unittest.main()
