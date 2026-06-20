"""FS1 (IP_PLANNER_MODES) — plan construction modes.

auto (default, unchanged) | fixed_core (1 HIT type/week, reps progress, Z2 core) |
template (preset blueprint). Checks: the mode is honoured, auto behaviour is
unchanged (diversification floors still inject variety), fixed_core/template keep
ONE HIT type per build week + 0 on deload, the plan is deterministic, and the
matcher/deload guards (B5/B3) still hold for blueprint plans. Hermetic (restores
the tracked library index).
"""
import unittest
from datetime import date, timedelta
from pathlib import Path

import training_planner as tp

_LIB_INDEX = Path(__file__).resolve().parent.parent / "workouts" / ".library_index.json"
_HIT = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}


def _goal(mode, tmpl="", weeks=16, hpw=10.0):
    return tp.Goal(
        goal_type="ftp", hours_per_week=hpw,
        max_weekday_hours=2.0, max_weekend_hours=4.0,
        available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[0],
        daily_max_hours={}, plan_weeks=weeks,
        plan_mode=mode, template_id=tmpl)


def _wk_tss(w):
    return sum((s.tss_estimate or 0) for s in w.sessions
               if s and s.session_type != "rest")


class TestPlannerModes(unittest.TestCase):
    def setUp(self):
        self._backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None
        self._if = {(w.get("File") or "").strip(): float(w.get("IF") or 0)
                    for w in tp.load_workout_library() if (w.get("File") or "").strip()}

    def tearDown(self):
        if self._backup is not None:
            _LIB_INDEX.write_bytes(self._backup)

    def test_default_is_auto(self):
        self.assertEqual(tp.Goal(goal_type="ftp").plan_mode, "auto")

    def test_fixed_core_one_hit_type_per_build_week(self):
        _ph, weeks = tp.generate_plan(_goal("fixed_core"), recent_weekly_tss=500)
        for w in weeks:
            hits = [s.session_type for s in w.sessions if s.session_type in _HIT]
            has_test = any(s.session_type == "ftp_test" for s in w.sessions)
            sb = getattr(w, "is_stepback", False) or w.phase == "taper"
            if sb:
                self.assertEqual(hits, [], f"W{w.week_num} deload has HIT {hits}")
            elif has_test:
                self.assertEqual(hits, [], f"W{w.week_num} ftp-test week + HIT {hits}")
            else:
                self.assertEqual(len(hits), 1, f"W{w.week_num} has {len(hits)} HIT {hits}")
                self.assertEqual(len(set(hits)), 1, f"W{w.week_num} mixed HIT {hits}")

    def test_fixed_core_is_deterministic(self):
        a = tp.generate_plan(_goal("fixed_core"), recent_weekly_tss=500)[1]
        b = tp.generate_plan(_goal("fixed_core"), recent_weekly_tss=500)[1]
        sig = lambda wks: [(w.week_num, s.session_type, s.duration_min)
                           for w in wks for s in w.sessions]
        self.assertEqual(sig(a), sig(b))

    def test_template_uses_preset_hit_types(self):
        _ph, weeks = tp.generate_plan(_goal("template", "ftp_builder"),
                                      recent_weekly_tss=500)
        used = {s.session_type for w in weeks for s in w.sessions
                if s.session_type in _HIT}
        # ftp_builder preset is sweet-spot / threshold only — never vo2max/sprint.
        self.assertTrue(used)
        self.assertEqual(used - {"sweetspot", "threshold"}, set(),
                         f"ftp_builder leaked off-preset HIT: {used}")

    def test_auto_still_diversifies(self):
        # The diversification floors must STILL run for auto — a build/peak phase
        # uses more than one HIT type (the gate is mode-specific, not global).
        _ph, weeks = tp.generate_plan(_goal("auto"), recent_weekly_tss=500)
        build_hits = {s.session_type for w in weeks
                      for s in w.sessions if s.session_type in _HIT
                      and w.phase in ("build1", "build2", "peak")}
        self.assertGreaterEqual(len(build_hits), 2,
                                f"auto should mix HIT types, got {build_hits}")

    def test_blueprint_keeps_b5_and_b3(self):
        # The blueprint reuses the post-passes: easy slots stay easy (B5) and the
        # deload is the lightest in its block (B3).
        _ph, weeks = tp.generate_plan(_goal("fixed_core"), recent_weekly_tss=500)
        # B5: no easy slot carries a too-hard file.
        for w in weeks:
            for s in w.sessions:
                if s.session_type in ("z2", "long_z2", "recovery"):
                    f = (getattr(s, "zwo_file", "") or "").strip()
                    if f:
                        self.assertLessEqual(self._if.get(f, 0.0),
                                             tp._EASY_SLOT_IF_CEILING,
                                             f"easy slot hard file {f}")
        # B3: each deload < lightest build in its block.
        for i, wk in enumerate(weeks):
            if not getattr(wk, "is_stepback", False) or wk.phase == "taper":
                continue
            builds, j = [], i - 1
            while j >= 0 and not getattr(weeks[j], "is_stepback", False):
                if weeks[j].phase != "taper":
                    builds.append(weeks[j])
                j -= 1
            if builds:
                self.assertLess(_wk_tss(wk), min(_wk_tss(b) for b in builds),
                                f"deload W{wk.week_num} not lightest")


if __name__ == "__main__":
    unittest.main()
