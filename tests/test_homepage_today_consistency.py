"""v4.6.0 IMPL-HOMEPAGE-CONSISTENCY — Pillar D contract tests
(MASTER_DECISIONS_v46.md §3 Pillar D + §4).

Asserts the /api/today-session response shape used by the homepage
"Today's recommendation" hero block, plus the JS-side defensive regex
that hides "0min @ X% FTP" tokens from rendered description text.

Tests:
  1. /api/today-session returns BOTH planned + adjusted fields when an
     adjustment ran (HRV-streak / readiness / yesterday-load triggers).
  2. adjustment_reason is populated with descriptive text on adjusted days.
  3. The 0min defensive regex (mirrored in fixZeroMin in dashboard.html)
     correctly strips "0min @ X% FTP" segments from a description string.
  4. Today's session lookup uses local-date arithmetic (not UTC),
     so a plan keyed off today's local date matches the endpoint's pick.
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
import ride_storage
import training_planner as tp


def _mk_today_plan_dict(today: date, session_type: str, duration_min: int,
                        tss: int, description: str) -> dict:
    """Build a single-week plan dict where today's session matches the args."""
    monday = today - timedelta(days=today.weekday())
    sessions = []
    for off in range(7):
        d = monday + timedelta(days=off)
        if d == today:
            sessions.append({
                "day": d.isoformat(),
                "day_name": d.strftime("%a"),
                "session_type": session_type,
                "duration_min": duration_min,
                "tss_estimate": tss,
                "description": description,
                "zwo_file": f"{session_type}_test.zwo",
                "zwo_name": f"{session_type} test",
                "status": "pending",
            })
        else:
            sessions.append({
                "day": d.isoformat(),
                "day_name": d.strftime("%a"),
                "session_type": "rest",
                "duration_min": 0,
                "tss_estimate": 0,
                "description": "Rest day",
                "zwo_file": "",
                "zwo_name": "",
                "status": "pending",
            })
    return {
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0, 4, 6]},
        "phases": [],
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "phase": "base",
            "tss_target": tss,
            "is_stepback": False,
            "sessions": sessions,
        }],
        "generated": "2026-05-03T00:00:00",
    }


class HomepageTodayConsistencyBase(unittest.TestCase):
    def setUp(self):
        app_module.clear_cache()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self._today = date.today()

        # Plant a tempo session for today so the endpoint has something to
        # adjust. The test scenarios then drive readiness/HRV to trigger
        # the Pillar D "adjusted to Z2 due to ..." branch.
        self._plan = _mk_today_plan_dict(
            self._today, "tempo", 75, 60, "3x15min @ 88% FTP")
        (self._tmp / "current_plan.json").write_text(json.dumps(self._plan))

        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp

        # ── 3.4.2 M7: hermetic sandbox ────────────────────────────────────
        # /api/today-session also reads the LIVE dev environment: the ride
        # archive (G2/G7 rides_recent, the DFA-cap scan, local-load fallback),
        # the LIVE db daily log (G6/G7 gates + soreness subjective), and the
        # LIVE C6 revert flag (DATA_DIR/readiness_cap_reverted.json — one
        # "Keep original" click on the dev machine suppressed EVERY
        # adjustment and turned this suite red). Pin each input to a tmp/
        # empty equivalent so the HRV-streak → Z2 condition the tests create
        # is the ONLY signal. Assertions unchanged.
        self._fit_dir = self._tmp / "rides"
        self._icu_dir = self._tmp / "rides" / "icu"
        self._icu_dir.mkdir(parents=True, exist_ok=True)
        self._patches = [
            # Block lazy ICU sync — tests mustn't hit the network.
            patch.object(app_module, "_maybe_lazy_icu_sync", return_value=None),
            # Empty activities so yesterday_tss_ratio = 1.0 (no load trigger
            # unless explicitly set in the test).
            patch.object(app_module.db, "query_activities", return_value=[]),
            # Empty ride archive via the ride_storage seams (tmp dirs): no
            # live ride can fire G2/G7, contribute a DFA cap, or feed the
            # local-load fallback.
            patch.object(ride_storage, "_icu_rides_dir", return_value=self._icu_dir),
            patch.object(ride_storage, "_fit_rides_dir", return_value=self._fit_dir),
            patch.object(app_module, "_rides_fit_dir", return_value=self._fit_dir),
            patch.object(ride_storage, "list_rides", return_value=[]),
            patch.object(app_module, "_load_all_rides_safe", return_value=[]),
            # No daily log today → G6/G7 quiet, soreness subjective None.
            patch.object(app_module.db, "get_daily_log_today", return_value=None),
            # C6 revert flag sandboxed to a nonexistent tmp file: the rider
            # did NOT click "Keep original" today, so adjustments stand.
            patch.object(
                app_module, "_readiness_revert_flag_path",
                return_value=self._tmp / "readiness_cap_reverted.json",
            ),
        ]
        for p in self._patches:
            p.start()

        self.client = TestClient(app_module.app)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()


class TestTodaySessionPlannedAndAdjusted(HomepageTodayConsistencyBase):
    """Pillar D: response carries BOTH planned + adjusted when adjustment ran."""

    def test_planned_and_adjusted_both_present_on_adjustment(self):
        # HRV streak day 1 below SWC + tempo planned → forces Z2 downgrade.
        with patch.object(
            app_module, "get_sleep_metrics",
            return_value={"red_hrv_streak": 1, "ln_rmssd_7d": None,
                          "swc_lower": None, "swc_upper": None,
                          "sleep_h": 7.5, "rhr_delta": 0},
        ), patch.object(
            app_module, "get_today_metrics",
            return_value={"ctl": 50, "atl": 45, "tsb": 5},
        ):
            r = self.client.get("/api/today-session")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()

            # planned = original tempo session.
            self.assertIsNotNone(data.get("planned"))
            self.assertEqual(data["planned"]["session_type"], "tempo")
            self.assertEqual(data["planned"]["duration_min"], 75)
            self.assertEqual(data["planned"]["tss_estimate"], 60)

            # adjusted = Z2 downgrade (HRV-streak day 1 rule).
            self.assertIsNotNone(data.get("adjusted"))
            self.assertEqual(data["adjusted"]["session_type"], "z2")
            self.assertTrue(data.get("was_modified"),
                            "was_modified should be True when adjusted")


class TestAdjustmentReasonPopulated(HomepageTodayConsistencyBase):
    """Pillar D: adjustment_reason carries descriptive text on adjusted days."""

    def test_adjustment_reason_field_present_and_descriptive(self):
        with patch.object(
            app_module, "get_sleep_metrics",
            return_value={"red_hrv_streak": 1, "ln_rmssd_7d": None,
                          "swc_lower": None, "swc_upper": None,
                          "sleep_h": 7.5, "rhr_delta": 0},
        ), patch.object(
            app_module, "get_today_metrics",
            return_value={"ctl": 50, "atl": 45, "tsb": 5},
        ):
            data = self.client.get("/api/today-session").json()

            # New canonical field per Pillar D.
            self.assertIn("adjustment_reason", data)
            reason = data["adjustment_reason"]
            self.assertIsInstance(reason, str)
            self.assertTrue(reason, "adjustment_reason must be non-empty when adjusted")
            # Descriptive: mentions HRV/readiness/load and the original type.
            lowered = reason.lower()
            self.assertTrue(
                any(k in lowered for k in ("hrv", "readiness", "tss", "z2", "tempo")),
                f"adjustment_reason should mention WHY: {reason!r}",
            )
            # reason (legacy) and adjustment_reason agree.
            self.assertEqual(data.get("reason"), reason)


class TestZeroMinRegexHidesSegments(unittest.TestCase):
    """Pillar D: the JS fixZeroMin defensive regex strips 0min @ X% FTP tokens.

    The browser-side function lives in dashboard.html. We replicate its
    semantics here to lock the contract — if the JS regex changes, this
    test must be updated in lockstep so the behaviour can't silently
    diverge.
    """

    @staticmethod
    def _fix_zero_min_py(desc: str) -> str:
        """Python mirror of the JS fixZeroMin() in templates/dashboard.html."""
        if not desc:
            return desc
        # Strip "Nmin @ N% FTP" only when the leading number is exactly 0.
        out = re.sub(
            r"(\d+)min @ (\d+)% FTP",
            lambda m: "" if m.group(1) == "0" else m.group(0),
            desc,
        )
        out = re.sub(r"\|\s*\|", "|", out)        # collapse "X | | Y"
        out = re.sub(r"\s*\|\s*$", "", out)         # trailing pipe
        out = re.sub(r"^\s*\|\s*", "", out)         # leading pipe
        out = re.sub(r"\s*\n\s*\n", "\n", out)      # blank lines
        return out.strip()

    def test_strips_zero_min_segments(self):
        desc = "10min @ 65% FTP | 0min @ 110% FTP | 5min @ 75% FTP"
        out = self._fix_zero_min_py(desc)
        # Only the standalone "0min @ N% FTP" gets stripped — "10min" must stay.
        self.assertNotRegex(out, r"\b0min @")
        self.assertIn("10min @ 65% FTP", out)
        self.assertIn("5min @ 75% FTP", out)

    def test_keeps_non_zero_min_segments(self):
        desc = "20min @ 88% FTP | 1min @ 110% FTP | 5min @ 65% FTP"
        out = self._fix_zero_min_py(desc)
        self.assertEqual(out, "20min @ 88% FTP | 1min @ 110% FTP | 5min @ 65% FTP")

    def test_handles_empty_and_no_match(self):
        self.assertEqual(self._fix_zero_min_py(""), "")
        self.assertEqual(self._fix_zero_min_py("Z2 endurance ride"), "Z2 endurance ride")

    def test_dashboard_html_exposes_fixzeromin_at_render_sites(self):
        """The four description-rendering call sites in dashboard.html must
        all wrap their description through fixZeroMin so a stray 0min token
        from the workout library can never reach the user's screen."""
        html = (Path(__file__).parent.parent / "templates" /
                "dashboard.html").read_text(encoding="utf-8")
        # Must define the helper.
        self.assertIn("function fixZeroMin", html,
                      "fixZeroMin helper missing from dashboard.html")
        # Must use it at every description render site we audited.
        # 1) workout modal description (~line 2406)
        # 2) homepage hero planned description
        # 3) homepage hero adjusted description (chip)
        # 4) Today's Session detail modal
        # 5) plan-grid session detail modal
        # We don't pin line numbers — count occurrences instead. With the
        # surgical Pillar D edits this should be ≥4 distinct callers.
        n = html.count("fixZeroMin(")
        self.assertGreaterEqual(
            n, 4,
            f"fixZeroMin() should be applied at ≥4 description render sites, found {n}",
        )


class TestTodayUsesLocalDate(HomepageTodayConsistencyBase):
    """Pillar D: today's-session lookup uses the local date, not UTC.

    The endpoint computes today_str = date.today().isoformat() and matches
    that against session.day. This test verifies the planted plan keyed
    off date.today() in setUp() is the one returned — locking in the
    local-date contract so a UTC drift wouldn't silently map "today"
    onto yesterday/tomorrow's session in non-UTC timezones.
    """

    def test_today_session_matches_local_date(self):
        # Default (no HRV/readiness triggers) → adjusted == planned.
        with patch.object(
            app_module, "get_sleep_metrics",
            return_value={"red_hrv_streak": 0, "ln_rmssd_7d": None,
                          "swc_lower": None, "swc_upper": None,
                          "sleep_h": 7.5, "rhr_delta": 0},
        ), patch.object(
            app_module, "get_today_metrics",
            return_value={"ctl": 50, "atl": 45, "tsb": 5},
        ):
            data = self.client.get("/api/today-session").json()
            self.assertIsNotNone(data.get("planned"),
                                 "planned must be populated for today")
            # The planted plan put a tempo on today's local date —
            # endpoint must surface it (not a neighbouring rest day).
            self.assertEqual(data["planned"]["session_type"], "tempo")


if __name__ == "__main__":
    unittest.main()
