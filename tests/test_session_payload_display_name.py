"""v1.0.4 IMPL-WIRING — session-payload contract tests for display_name.

Asserts the dashboard-facing endpoints surface the new Layer 3 fields
(``display_name`` + ``zwo_duration_min``) introduced by MASTER §3 of
``/tmp/MASTER_DECISIONS_v104.md``. The fields are read from the new
``display_name`` key inside each ``.content_classification.json`` entry
(produced by IMPL-CLASSIFIER-V104) and from the library row's
``Duration(min)`` (already computed by ``training_planner.load_workout_library``
as the sum of segment durations).

The dashboard cascade for the modal title is::

    1. session.display_name  (preferred — Layer 3 from JSON)
    2. session.zwo_name      (fallback — ZWO ``<name>`` tag)
    3. session.session_type  (last-resort — planner intent)

These tests must NOT depend on the live ``.content_classification.json``
because IMPL-CLASSIFIER-V104 hasn't necessarily rewritten it yet at the
time this agent commits. Both the classification loader and the library
loader are monkeypatched with fixture data instead.

Three locked tests:

  1. test_plan_payload_includes_display_name_field
       — ``/api/plan`` annotates every session with ``display_name`` +
         ``zwo_duration_min``, even when the field is empty.
  2. test_plan_payload_uses_classification_display_name
       — when the matched library file has a ``display_name`` in the
         classification fixture, the session payload reflects it
         verbatim (and ``zwo_duration_min`` reflects the library's
         ``Duration(min)``).
  3. test_plan_payload_freeform_session_has_empty_display_name
       — a session with no ``zwo_file`` (free-form, e.g. rest day or
         unmatched intent) gets ``display_name == ""`` and
         ``zwo_duration_min == 0``. The endpoint does not crash.
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


# ── Fixtures ──────────────────────────────────────────────────────────────
#
# Synthetic classification + library entries — all dashboard-facing fields
# locked at MASTER §3. Two ZWO files: one matched (with display_name), one
# matched (without display_name in the JSON to assert the empty-fallback path).

_FAKE_CLASSIFICATIONS = {
    "tempo_steady_57min.zwo": {
        # MASTER §3 canary: this file's stem says "tempo" but the content is
        # actually a threshold ladder. The display_name is the truth.
        "primary": "threshold_ladder",
        "display_name": "Threshold Ladder 58min — 85->97% x 4",
    },
    "endurance_60min.zwo": {
        "primary": "endurance",
        # display_name absent → fixture exercises the "" fallback even
        # when the entry exists in the classification JSON.
    },
}

_FAKE_LIBRARY = [
    {
        "File": "tempo_steady_57min.zwo",
        "Name": "Tempo (58min)",  # mis-leading <name> tag — the canary
        "Duration(min)": 58.0,
        "TSS": 70.0, "IF": 0.85, "Score": 7,
        "Z1%": 0, "Z2%": 10, "Z3%": 20, "Z4%": 60, "Z5%": 10, "Z6%": 0,
        "Protocol": "Threshold",
    },
    {
        "File": "endurance_60min.zwo",
        "Name": "Endurance (60min)",
        "Duration(min)": 60.0,
        "TSS": 50.0, "IF": 0.65, "Score": 5,
        "Z1%": 5, "Z2%": 90, "Z3%": 5, "Z4%": 0, "Z5%": 0, "Z6%": 0,
        "Protocol": "Endurance",
    },
]


def _build_plan_with_sessions(monday: date) -> dict:
    """Plant a 1-week plan with three session shapes:

      - Mon: matched ZWO with display_name in fixture (canary).
      - Tue: matched ZWO without display_name (asserts "" fallback).
      - Wed: rest day — no zwo_file at all (asserts free-form path).
    """
    sessions = [
        {
            "day": (monday + timedelta(days=0)).isoformat(),
            "day_name": "Mon",
            "session_type": "tempo",  # planner intent
            "duration_min": 60,
            "tss_estimate": 65,
            "description": "tempo 60min",
            "zwo_file": "tempo_steady_57min.zwo",
            "zwo_name": "Tempo (58min)",
            "status": "pending",
        },
        {
            "day": (monday + timedelta(days=1)).isoformat(),
            "day_name": "Tue",
            "session_type": "z2",
            "duration_min": 60,
            "tss_estimate": 50,
            "description": "endurance 60min",
            "zwo_file": "endurance_60min.zwo",
            "zwo_name": "Endurance (60min)",
            "status": "pending",
        },
        {
            "day": (monday + timedelta(days=2)).isoformat(),
            "day_name": "Wed",
            "session_type": "rest",
            "duration_min": 0,
            "tss_estimate": 0,
            "description": "",
            "zwo_file": "",
            "zwo_name": "",
            "status": "pending",
        },
    ]
    return {
        "goal": {"type": "general", "hours_per_week": 8.0,
                 "rest_days": [2, 6]},
        "phases": [{"name": "base", "start": monday.isoformat(),
                    "end": (monday + timedelta(days=6)).isoformat(),
                    "weeks": 1, "focus": "endurance",
                    "weekly_tss": 200, "hit_per_week": 1}],
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "phase": "base",
            "tss_target": 200,
            "is_stepback": False,
            "sessions": sessions,
        }],
        "generated": "2026-05-05T00:00:00",
    }


class _BasePayloadTest(unittest.TestCase):
    """Shared TestClient + plan-dir + fixture wiring."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        today = date.today()
        self._monday = today - timedelta(days=today.weekday())
        plan = _build_plan_with_sessions(self._monday)
        (self._tmp / "current_plan.json").write_text(json.dumps(plan))

        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp

        # Inject fixtures so the test does not depend on the live
        # workouts/.content_classification.json or the live workout library.
        # The classifier loader is module-level on tp; library is also tp.
        self._patches = [
            patch.object(
                tp, "_load_content_classifications",
                return_value=_FAKE_CLASSIFICATIONS,
            ),
            patch.object(
                tp, "load_workout_library",
                return_value=_FAKE_LIBRARY,
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

    def _sessions_from_plan(self) -> list[dict]:
        resp = self.client.get("/api/plan")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        plan_json = body.get("plan_json") or {}
        all_sessions: list[dict] = []
        for w in plan_json.get("weeks", []) or []:
            for s in w.get("sessions", []) or []:
                all_sessions.append(s)
        return all_sessions


class TestPlanPayloadIncludesDisplayNameField(_BasePayloadTest):
    """Test 1: every session in /api/plan carries display_name + zwo_duration_min,
    regardless of whether the field is populated."""

    def test_plan_payload_includes_display_name_field(self):
        sessions = self._sessions_from_plan()
        # The plan plants exactly 3 sessions (Mon, Tue, Wed) — assert the API
        # surfaces them and every one carries the new fields.
        self.assertEqual(len(sessions), 3,
                         f"expected 3 sessions, got {len(sessions)}")
        for s in sessions:
            self.assertIn("display_name", s,
                          f"session {s.get('day')} missing display_name")
            self.assertIn("zwo_duration_min", s,
                          f"session {s.get('day')} missing zwo_duration_min")
            # Fields exist; their VALUES are asserted in the next two tests.
            self.assertIsInstance(s["display_name"], str)
            self.assertIsInstance(s["zwo_duration_min"], int)


class TestPlanPayloadUsesClassificationDisplayName(_BasePayloadTest):
    """Test 2: when the matched library file has a display_name in the
    classification fixture, that exact string flows through to the payload.
    zwo_duration_min reflects the library row's Duration(min)."""

    def test_plan_payload_uses_classification_display_name(self):
        sessions = self._sessions_from_plan()
        mon_iso = self._monday.isoformat()
        mon = next((s for s in sessions if s.get("day") == mon_iso), None)
        self.assertIsNotNone(mon, "Mon session must surface in /api/plan")
        # Canary: classification fixture says display_name = "Threshold Ladder…".
        self.assertEqual(
            mon["display_name"],
            "Threshold Ladder 58min — 85->97% x 4",
            "display_name must come verbatim from the classification fixture",
        )
        # zwo_duration_min must come from the library's Duration(min) (58.0
        # in the fixture, rounded to int).
        self.assertEqual(mon["zwo_duration_min"], 58)
        # Sanity: zwo_file is the matched file, zwo_name is the misleading
        # ZWO <name> tag — display_name is the truth.
        self.assertEqual(mon["zwo_file"], "tempo_steady_57min.zwo")
        self.assertEqual(mon["zwo_name"], "Tempo (58min)")
        self.assertNotEqual(mon["display_name"], mon["zwo_name"])


class TestPlanPayloadFreeformSessionHasEmptyDisplayName(_BasePayloadTest):
    """Test 3: a session with no zwo_file (rest day, free-form intent) gets
    ``display_name == ""`` and ``zwo_duration_min == 0``. The endpoint does
    not crash. Also asserts that a matched session whose classification
    entry omits display_name also yields "" — exercising both branches of
    the graceful-degradation contract."""

    def test_plan_payload_freeform_session_has_empty_display_name(self):
        sessions = self._sessions_from_plan()
        wed_iso = (self._monday + timedelta(days=2)).isoformat()
        wed = next((s for s in sessions if s.get("day") == wed_iso), None)
        self.assertIsNotNone(wed, "Wed session must surface in /api/plan")
        # Rest day — no zwo_file at all → empty display_name + 0 duration.
        self.assertEqual(wed["zwo_file"], "")
        self.assertEqual(wed["display_name"], "",
                         "free-form session must yield empty display_name")
        self.assertEqual(wed["zwo_duration_min"], 0,
                         "free-form session must yield 0 zwo_duration_min")

        # Also assert the "matched-but-no-display_name-in-JSON" path:
        # endurance_60min.zwo's classification entry deliberately omits
        # display_name — the API should still emit the field as "".
        tue_iso = (self._monday + timedelta(days=1)).isoformat()
        tue = next((s for s in sessions if s.get("day") == tue_iso), None)
        self.assertIsNotNone(tue, "Tue session must surface in /api/plan")
        self.assertEqual(tue["zwo_file"], "endurance_60min.zwo")
        self.assertEqual(
            tue["display_name"], "",
            "matched session whose classification has no display_name "
            "must yield empty display_name (graceful degradation)",
        )
        # zwo_duration_min still flows through from the library row.
        self.assertEqual(tue["zwo_duration_min"], 60)


class TestWeeklyPlanPayloadIncludesDisplayName(_BasePayloadTest):
    """Test 4 (extra): /api/weekly-plan also surfaces display_name +
    zwo_duration_min on each session. The endpoint is what /api/today-session
    consumes for the today card, so the cascade must work there too.

    Note: /api/weekly-plan regenerates the weekly tile on-the-fly via
    tp.generate_weekly_plan, then merges stored fields by date overlap. The
    display_name lookup happens after the merge — so the field is present
    on every session whose stored zwo_file matches a fixture entry.
    """

    def test_weekly_plan_payload_includes_display_name(self):
        resp = self.client.get("/api/weekly-plan?week_offset=0")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        sessions = body.get("sessions") or []
        self.assertGreaterEqual(len(sessions), 7,
                                "weekly plan should emit a Mon-Sun grid")
        # Every session must carry the new fields; values may be empty when
        # zwo_file isn't matched (e.g. on regenerated rest days).
        for s in sessions:
            self.assertIn("display_name", s,
                          f"weekly-plan session {s.get('day')} missing display_name")
            self.assertIn("zwo_duration_min", s,
                          f"weekly-plan session {s.get('day')} missing zwo_duration_min")
            self.assertIsInstance(s["display_name"], str)
            self.assertIsInstance(s["zwo_duration_min"], int)


class TestCalendarPlannedPayloadIncludesDisplayName(_BasePayloadTest):
    """Test 5 (extra): /api/calendar's per-day `planned` block carries
    display_name + zwo_duration_min so the calendar cell label can read
    the canonical title."""

    def test_calendar_planned_payload_includes_display_name(self):
        resp = self.client.get("/api/calendar")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        any_with_dn = False
        any_field_present = 0
        for w in body.get("weeks", []) or []:
            for day in w.get("days", []) or []:
                planned = day.get("planned")
                if planned is None:
                    continue
                self.assertIn("display_name", planned,
                              f"calendar planned cell {day.get('date')} "
                              f"missing display_name")
                self.assertIn("zwo_duration_min", planned,
                              f"calendar planned cell {day.get('date')} "
                              f"missing zwo_duration_min")
                any_field_present += 1
                if planned["display_name"]:
                    any_with_dn = True
        self.assertGreaterEqual(any_field_present, 1,
                                "calendar must surface at least one planned cell")
        self.assertTrue(
            any_with_dn,
            "Mon canary planned cell must propagate display_name through "
            "to /api/calendar (Threshold Ladder fixture entry)",
        )


if __name__ == "__main__":
    unittest.main()
