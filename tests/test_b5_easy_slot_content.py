"""B5 (tester reliability) — an easy slot must carry an EASY file.

The planner's prescription (slot type/duration) and the matched library .zwo were
decoupled: the sampler buckets by content_class, so an interval-structured file
the classifier filed as "endurance" (high IF, e.g. "Endurance 20s/2min 6x") — or
a sweet-spot / over-under file — could land on a z2/long_z2/recovery slot. The
tester saw a Z2 60-min slot matched to a 196-TSS interval file.

Fix: match_zwo enforces _EASY_SLOT_IF_CEILING, and the final
_enforce_easy_slot_content pass re-routes any leaked easy slot through it +
recomputes the slot's TSS. Invariant: no easy slot carries a file above the
ceiling. A few seeds + event configs; restores the tracked library index.
"""
import unittest
from datetime import date, timedelta
from pathlib import Path

import training_planner as tp

_LIB_INDEX = Path(__file__).resolve().parent.parent / "workouts" / ".library_index.json"
_EASY = {"z2", "long_z2", "recovery"}


def _egoal(weeks, hpw):
    return tp.Goal(
        goal_type="event", target_date=date.today() + timedelta(weeks=weeks),
        target_ctl=85, hours_per_week=hpw,
        max_weekday_hours=2.0, max_weekend_hours=4.0,
        available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[0],
        daily_max_hours={}, plan_weeks=weeks,
        event_km=140, event_climb_m=1500, event_type="gran_fondo",
    )


class TestEasySlotContent(unittest.TestCase):
    def setUp(self):
        self._backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None
        lib = tp.load_workout_library()
        self._if = {(w.get("File") or "").strip(): float(w.get("IF") or 0)
                    for w in lib if (w.get("File") or "").strip()}

    def tearDown(self):
        if self._backup is not None:
            _LIB_INDEX.write_bytes(self._backup)

    def test_no_easy_slot_carries_a_hard_file(self):
        for salt in range(6):
            with self.subTest(seed=salt):
                _ph, weeks = tp.generate_plan(
                    _egoal(24, 10.0), seed_salt=salt, recent_weekly_tss=500)
                for w in weeks:
                    for s in w.sessions:
                        if s.session_type not in _EASY:
                            continue
                        f = (getattr(s, "zwo_file", "") or "").strip()
                        if not f:
                            continue
                        self.assertLessEqual(
                            self._if.get(f, 0.0), tp._EASY_SLOT_IF_CEILING,
                            f"easy slot {s.session_type} {s.duration_min}min "
                            f"matched hard file {f} "
                            f"(IF {self._if.get(f, 0.0):.2f})")

    def test_easy_slot_tss_stays_in_the_easy_band(self):
        # An easy slot's TSS must never exceed what the IF ceiling implies for its
        # duration (TSS = hours · IF² · 100). This is the physical bound that
        # rejects the tester's 196-TSS Z2 (a 60-min easy slot tops out at ≈61
        # TSS at the 0.78 ceiling), while allowing a genuine firm-but-easy file.
        _ph, weeks = tp.generate_plan(
            _egoal(16, 10.0), seed_salt=3, recent_weekly_tss=500)
        for w in weeks:
            for s in w.sessions:
                if s.session_type not in _EASY or (s.duration_min or 0) <= 0:
                    continue
                max_easy_tss = (s.duration_min / 60) * tp._EASY_SLOT_IF_CEILING ** 2 * 100
                self.assertLessEqual(
                    s.tss_estimate, max_easy_tss + 5,
                    f"easy {s.session_type} {s.duration_min}min has "
                    f"TSS {s.tss_estimate} > easy ceiling {max_easy_tss:.0f}")


if __name__ == "__main__":
    unittest.main()
