"""v4.6.7 IMPL-UX — 4 surgical UX fixes (F1..F4).

Verifies the contracts established in MASTER_DECISIONS_v467 §4 and the
audit fixes in /tmp/audit_ux_fixes.md:

  F1 Cooldown ramp now downslopes (workoutProfileSVG was drawing the ZWO
     Cooldown PowerLow→PowerHigh L→R, but PowerLow is the END value).
     Verified by string-level inspection of the renderer.
  F2 Yellow ⟳ (card_state=missing_workout) sessions are clickable
     (calOpenDay no longer early-returns; falls through to synth render).
     Verified by string-level inspection of calOpenDay.
  F3 Calendar auto-scrolls to today on first paint via calJumpToToday().
     Verified by string-level inspection of the requestAnimationFrame
     post-render block.
  F4 Event-day green border + end-of-plan marker — /api/calendar emits
     goal.{type, event_date, end_date}; dashboard renders .cal-event-day.
     Verified live against the FastAPI TestClient + dashboard CSS+JS
     string-level inspection.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


DASH_PATH = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"


def _mk_plan_weeks(monday: date, weeks_count: int = 2) -> list[dict]:
    weeks = []
    for w_idx in range(weeks_count):
        wstart = monday + timedelta(weeks=w_idx)
        sessions = []
        for off in range(7):
            d = wstart + timedelta(days=off)
            sessions.append({
                "day": d.isoformat(),
                "day_name": d.strftime("%a"),
                "session_type": "rest" if off in (0, 4, 6) else "z2",
                "duration_min": 0 if off in (0, 4, 6) else 60,
                "tss_estimate": 0 if off in (0, 4, 6) else 45,
                "description": "z2 60min" if off not in (0, 4, 6) else "rest",
                "zwo_file": "" if off in (0, 4, 6) else "z2_test.zwo",
                "zwo_name": "" if off in (0, 4, 6) else "z2 test",
                "status": "pending",
            })
        weeks.append({
            "week_num": w_idx + 1,
            "start": wstart.isoformat(),
            "end": (wstart + timedelta(days=6)).isoformat(),
            "phase": "base",
            "tss_target": 270,
            "is_stepback": False,
            "sessions": sessions,
        })
    return weeks


class _CalendarUxBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        today = date.today()
        self._monday = today - timedelta(days=today.weekday())
        self._weeks = _mk_plan_weeks(self._monday, weeks_count=2)
        self._end_date = self._weeks[-1]["end"]

        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp

        self._fit_dir = self._tmp / "fit"
        self._fit_dir.mkdir(parents=True, exist_ok=True)
        self._patch_fit = patch.object(
            app_module, "_rides_fit_dir", return_value=self._fit_dir
        )
        self._patch_fit.start()

        import ride_storage as _rs
        self._icu_dir = self._tmp / "icu"
        self._icu_dir.mkdir(parents=True, exist_ok=True)
        self._patch_icu_dir = patch.object(
            _rs, "_icu_rides_dir", return_value=self._icu_dir
        )
        self._patch_icu_dir.start()
        self._patch_fit_dir_rs = patch.object(
            _rs, "_fit_rides_dir", return_value=self._fit_dir
        )
        self._patch_fit_dir_rs.start()
        self._patch_rides = patch("ride_storage.list_rides", return_value=[])
        self._patch_rides.start()

        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch_rides.stop()
        self._patch_fit_dir_rs.stop()
        self._patch_icu_dir.stop()
        self._patch_fit.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def _write_plan(self, plan_dict: dict) -> None:
        (self._tmp / "current_plan.json").write_text(json.dumps(plan_dict))


class TestCalendarGoalBlock(_CalendarUxBase):
    """F4 — /api/calendar emits goal.{type, event_date, end_date}."""

    def test_calendar_goal_block_in_response_event_preparation(self):
        # Event in 6 weeks → goal.type='event_preparation', event_date set.
        event_d = (self._monday + timedelta(weeks=6)).isoformat()
        plan = {
            "goal": {
                "type": "event_preparation",
                "event_date": event_d,
                "event_name": "Gran Fondo X",
                "event_km": 120,
                "event_climb": 1500,
                "hours_per_week": 8.0,
            },
            "phases": [],
            "weeks": self._weeks,
            "generated": "2026-04-19T00:00:00",
        }
        self._write_plan(plan)
        r = self.client.get("/api/calendar")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn("goal", data, "v4.6.7 F4 contract: response.goal must exist")
        g = data["goal"]
        self.assertEqual(g["type"], "event_preparation")
        self.assertEqual(g["event_date"], event_d)
        # end_date is always populated when there are weeks.
        self.assertEqual(g["end_date"], self._end_date)

    def test_calendar_goal_block_weeks_goal(self):
        # Plain weeks goal → goal.event_date is None, end_date == last week.
        plan = {
            "goal": {
                "type": "weeks",
                "hours_per_week": 8.0,
            },
            "phases": [],
            "weeks": self._weeks,
            "generated": "2026-04-19T00:00:00",
        }
        self._write_plan(plan)
        r = self.client.get("/api/calendar")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn("goal", data)
        g = data["goal"]
        self.assertEqual(g["type"], "weeks")
        self.assertIsNone(g["event_date"])
        self.assertEqual(g["end_date"], self._end_date)


class TestDashboardJsAndCss(unittest.TestCase):
    """F1 + F2 + F3 + F4 — string-level dashboard.html inspection."""

    @classmethod
    def setUpClass(cls):
        cls._html = DASH_PATH.read_text(encoding="utf-8")

    def test_cooldown_segment_renders_downsloping(self):
        # F1 (v4.6.8 value-based fix) — workoutProfileSVG must render Cooldown
        # as a DOWN slope regardless of the ZWO file's PowerLow/PowerHigh
        # attribute ordering. The library is split: ~853 cooldowns use
        # PowerLow < PowerHigh (numerical-name convention), ~1399 use
        # PowerLow > PowerHigh (start/end convention). v4.6.7's tag-only
        # swap fixed group 1 and broke group 2; v4.6.8 swaps based on
        # VALUE: start = max(power_low, power_high), end = min(...) for
        # cooldown; opposite for warmup/ramp.
        self.assertIn("isCooldown", self._html,
                      "F1: workoutProfileSVG must branch on isCooldown")
        self.assertRegex(
            self._html,
            r"isCooldown\s*=\s*\(s\.type\s*===\s*'Cooldown'\)",
            "F1: must compare s.type === 'Cooldown'",
        )
        # Value-based selection: hi/lo computed via Math.max/Math.min over
        # the two power attributes; cooldown picks hi at start, lo at end.
        self.assertRegex(
            self._html,
            r"const\s+hi\s*=\s*Math\.max\(s\.power_low,\s*s\.power_high\)",
            "F1: must compute hi = max(power_low, power_high)",
        )
        self.assertRegex(
            self._html,
            r"const\s+lo\s*=\s*Math\.min\(s\.power_low,\s*s\.power_high\)",
            "F1: must compute lo = min(power_low, power_high)",
        )
        self.assertRegex(
            self._html,
            r"yStart\s*=\s*yScale\(\s*isCooldown\s*\?\s*hi\s*:\s*lo\s*\)",
            "F1: yStart = hi for Cooldown (slopes DOWN), lo for Warmup/Ramp (slopes UP)",
        )

    def test_yellow_arrow_session_clickable(self):
        # F2 — the early-return on missing_workout in calOpenDay() must be
        # gone; the comment must mention F2 (or fall-through). The synth
        # block must be reachable when planned is missing for missing_workout.
        # 1) The old early-return must NOT exist with the missing_workout block
        # returning before the synth-render. We assert the calOpenDay function
        # no longer has `if (cs === 'missing_workout') { ... return; }`.
        m = re.search(
            r"if\s*\(cs\s*===\s*'missing_workout'\)\s*\{[^}]*return;[^}]*\}",
            self._html, re.DOTALL,
        )
        self.assertIsNone(
            m,
            "F2: calOpenDay must NOT early-return on missing_workout. "
            f"Found: {m.group(0)[:120] if m else 'none'}",
        )
        # 2) The fall-through guard must allow planned-null in missing_workout.
        self.assertIn(
            "cs !== 'missing_workout'",
            self._html,
            "F2: the no-zwo guard must skip itself when cs === 'missing_workout'",
        )
        # 3) The synth block must defensively use day-level fallbacks.
        self.assertRegex(
            self._html,
            r"session_type:\s*p\.session_type\s*\|\|\s*day\.session_type",
            "F2: synth must fall back to day.session_type when planned is null",
        )

    def test_calendar_jumps_to_today_on_first_paint(self):
        # F3 — the auto-scroll block in renderCalendar must call
        # calJumpToToday() from inside requestAnimationFrame. Old code
        # used inline offsetTop math (read 0 before #cal-body laid out).
        # We slice from the marker line to the end of the function and
        # assert the post-paint hook calls calJumpToToday.
        idx = self._html.find("if (!window._calDidAutoScroll && curIdx >= 0)")
        self.assertGreater(idx, 0, "F3: auto-scroll guard missing")
        # Take the next ~400 chars — the whole block fits.
        block = self._html[idx:idx + 600]
        self.assertIn("requestAnimationFrame", block,
                      "F3: requestAnimationFrame missing in auto-scroll block")
        self.assertIn("calJumpToToday", block,
                      "F3: requestAnimationFrame block must call calJumpToToday()")
        # Sanity: the inline offsetTop math must NOT be present in this block.
        self.assertNotIn("scroller.scrollTop = Math.max", block,
                         "F3: inline offsetTop math must be removed")

    def test_event_day_css_and_render_class(self):
        # F4 — .cal-event-day CSS exists with green border, and renderCalDay
        # sets the class when d.date matches goal.event_date or goal.end_date.
        self.assertRegex(
            self._html,
            r"\.cal-day\.cal-event-day\s*\{[^}]*border:\s*2px\s+solid",
            "F4: .cal-day.cal-event-day CSS must define a 2px solid green border",
        )
        # Class is added to stateCls when isEventDay is true.
        self.assertIn(
            "cal-event-day",
            self._html,
            "F4: cal-event-day class must be referenced",
        )
        self.assertIn(
            "isEventDay",
            self._html,
            "F4: renderCalDay must compute isEventDay",
        )


if __name__ == "__main__":
    unittest.main()
