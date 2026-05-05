"""v1.0.3 fix-forward(workout-detail UX) — FIT export must mirror the matched
ZWO library file when ``zwo_file`` is supplied.

User report: clicking Download FIT for a session that resolved to
``tempo_steady_57min.zwo`` (a 21-block ladder) was returning a generic Tempo
block keyed only on ``(session_type, duration_min)`` — the ZWO and the FIT
were therefore the same workout in name only. These tests pin that the new
``zwo_file=`` query parameter:

1. Parses a real ZWO from ``WORKOUT_DIR`` and produces a FIT with one
   ``WorkoutStepMessage`` per parsed element (counting both halves of each
   ``IntervalsT`` repeat).
2. The first step's duration matches the ZWO's first ``<Warmup>`` / lead-in
   element duration (in seconds), and the last step's duration matches the
   ZWO's final ``<Cooldown>`` element duration.
3. A missing ``zwo_file`` returns 404 (loud failure — not a silent fallback
   to the generic block, which was the original bug).
"""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKOUTS_DIR = REPO_ROOT / "workouts"
TEMPO_FILE = "tempo_steady_57min.zwo"


def _zwo_step_count(zwo_path: Path) -> int:
    """Count FIT steps the parser will emit. Mirrors the dispatch logic in
    ``_build_fit_workout_from_zwo`` — keep them in lockstep."""
    tree = ET.parse(zwo_path)
    workout_el = tree.getroot().find("workout")
    if workout_el is None:
        return 0
    n = 0
    for el in workout_el:
        if el.tag in ("SteadyState", "Warmup", "Ramp", "Cooldown", "FreeRide"):
            n += 1
        elif el.tag == "IntervalsT":
            n += 2 * int(el.get("Repeat", 1))
    return n


def _decode_workout_steps(fit_bytes: bytes):
    """Return list of WorkoutStepMessage from a FIT byte payload."""
    from fit_tool.fit_file import FitFile
    from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage

    ff = FitFile.from_bytes(fit_bytes)
    return [r.message for r in ff.records if isinstance(r.message, WorkoutStepMessage)]


class TestFitFromZwo(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app)
        self.zwo_path = WORKOUTS_DIR / TEMPO_FILE
        if not self.zwo_path.exists():
            self.skipTest(f"library file {TEMPO_FILE} missing")

    def test_step_count_matches_zwo_elements(self):
        """The FIT should have exactly one step per parsed ZWO element
        (with IntervalsT contributing 2*Repeat steps). Pre-fix, the generic
        path produced a fixed Warmup/SteadyState/Cooldown 3-step regardless
        of the ZWO contents."""
        r = self.client.get(
            f"/api/export/fit-workout?session_type=z2&duration_min=57&name=test&zwo_file={TEMPO_FILE}"
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.headers["content-type"], "application/octet-stream")
        steps = _decode_workout_steps(r.content)
        expected = _zwo_step_count(self.zwo_path)
        self.assertEqual(len(steps), expected,
                         f"FIT has {len(steps)} steps, ZWO would emit {expected}")
        # tempo_steady_57min.zwo currently has 1 Warmup + 20 SteadyState + 1
        # Cooldown = 22 steps. The check above is the source of truth; assert
        # >= 5 here as a smoke check that we're well past the 3-block generic.
        self.assertGreaterEqual(len(steps), 5)

    def test_first_and_last_step_duration_match_zwo_envelope(self):
        """First step duration = ZWO's first element ``Duration`` (seconds);
        last step duration = ZWO's last element ``Duration``."""
        tree = ET.parse(self.zwo_path)
        workout_el = tree.getroot().find("workout")
        children = list(workout_el)
        first_dur_s = int(float(children[0].get("Duration", 0) or 0))
        last_dur_s = int(float(children[-1].get("Duration", 0) or 0))

        r = self.client.get(
            f"/api/export/fit-workout?session_type=z2&duration_min=57&name=test&zwo_file={TEMPO_FILE}"
        )
        self.assertEqual(r.status_code, 200)
        steps = _decode_workout_steps(r.content)
        self.assertGreaterEqual(len(steps), 2)
        # FIT duration_value is milliseconds.
        self.assertEqual(steps[0].duration_value, first_dur_s * 1000)
        self.assertEqual(steps[-1].duration_value, last_dur_s * 1000)

    def test_missing_zwo_file_returns_404_not_silent_fallback(self):
        """The pre-fix code silently used the generic block whenever the
        named ZWO wasn't found. The fix-forward fails loudly so the user
        notices and the bug doesn't return as a regression."""
        r = self.client.get(
            "/api/export/fit-workout?session_type=z2&duration_min=57&name=test&zwo_file=does_not_exist.zwo"
        )
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self.assertIn("not found", body.get("error", "").lower())


class TestFitGenericPathStillWorks(unittest.TestCase):
    """Regression — when ``zwo_file`` is omitted the generic generator must
    keep working (used by the fallback button at L10185 in dashboard.html
    when no library match is available)."""

    def setUp(self):
        self.client = TestClient(app_module.app)

    def test_generic_path_unaffected(self):
        r = self.client.get(
            "/api/export/fit-workout?session_type=z2&duration_min=75&name=Generic"
        )
        self.assertEqual(r.status_code, 200)
        steps = _decode_workout_steps(r.content)
        self.assertGreaterEqual(len(steps), 3)


if __name__ == "__main__":
    unittest.main()
