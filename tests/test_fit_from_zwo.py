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


def _zwo_element_min_steps(zwo_path: Path) -> int:
    """Minimum FIT steps the parser emits, counting each element as ≥1.
    v1.8.17: Warmup/Ramp/Cooldown now STAIRCASE into multiple sub-steps, so
    the FIT step count is ≥ this floor (no longer an exact 1:1)."""
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


def _zwo_total_seconds(zwo_path: Path) -> int:
    tree = ET.parse(zwo_path)
    workout_el = tree.getroot().find("workout")
    if workout_el is None:
        return 0
    t = 0
    for el in workout_el:
        d = int(float(el.get("Duration", 0) or 0))
        if el.tag == "IntervalsT":
            r = int(el.get("Repeat", 1))
            on = int(float(el.get("OnDuration", 0) or 0))
            off = int(float(el.get("OffDuration", 0) or 0))
            t += r * (on + off)
        else:
            t += d
    return t


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

    def test_step_count_at_least_one_per_zwo_element(self):
        """The FIT must have ≥1 step per parsed ZWO element (IntervalsT → 2×
        Repeat). v1.8.17: Warmup/Ramp/Cooldown STAIRCASE into sub-steps so the
        count is ≥ the element floor, not exactly equal. Pre-fix the generic
        path produced a fixed 3-step block regardless of ZWO contents."""
        r = self.client.get(
            f"/api/export/fit-workout?session_type=z2&duration_min=57&name=test&zwo_file={TEMPO_FILE}"
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.headers["content-type"], "application/octet-stream")
        steps = _decode_workout_steps(r.content)
        floor = _zwo_element_min_steps(self.zwo_path)
        self.assertGreaterEqual(len(steps), floor,
                                f"FIT has {len(steps)} steps, ZWO floor is {floor}")
        self.assertGreaterEqual(len(steps), 5)

    def test_total_duration_preserved_through_staircase(self):
        """v1.8.17 — the staircase of Warmup/Ramp/Cooldown into sub-steps MUST
        conserve the workout's total duration exactly (the sub-step durations
        sum back to the original element duration). This is the real
        ZWO≡FIT invariant now that step count is no longer 1:1."""
        r = self.client.get(
            f"/api/export/fit-workout?session_type=z2&duration_min=57&name=test&zwo_file={TEMPO_FILE}"
        )
        self.assertEqual(r.status_code, 200)
        steps = _decode_workout_steps(r.content)
        self.assertGreaterEqual(len(steps), 2)
        fit_total_s = sum((s.duration_value or 0) for s in steps) / 1000.0
        zwo_total_s = _zwo_total_seconds(self.zwo_path)
        self.assertEqual(round(fit_total_s), zwo_total_s,
                         f"FIT total {fit_total_s}s != ZWO total {zwo_total_s}s")

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


def _decode_file_id(fit_bytes: bytes):
    from fit_tool.fit_file import FitFile
    from fit_tool.profile.messages.file_id_message import FileIdMessage

    ff = FitFile.from_bytes(fit_bytes)
    ids = [r.message for r in ff.records if isinstance(r.message, FileIdMessage)]
    return ids[0] if ids else None


class TestFitHasTimeCreated(unittest.TestCase):
    """Regression — TrainingPeaks / Vekta / Garmin Connect REQUIRE
    file_id.time_created; a workout FIT without it imports as EMPTY. Both export
    paths (ZWO transcode + generic block builder) previously omitted it."""

    def setUp(self):
        self.client = TestClient(app_module.app)

    def _time_created(self, url: str) -> int:
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, r.content)
        fid = _decode_file_id(r.content)
        self.assertIsNotNone(fid, "FIT has no FileIdMessage")
        tc = next((f.get_value() for f in fid.fields if f.name == "time_created"), None)
        return tc

    def test_generic_path_sets_time_created(self):
        tc = self._time_created(
            "/api/export/fit-workout?session_type=vo2max&duration_min=60&name=T"
        )
        self.assertIsNotNone(tc, "generic FIT missing file_id.time_created")
        # Sane: a positive epoch-ms within the modern era (after 2020-01-01).
        self.assertGreater(tc, 1_577_836_800_000)

    def test_zwo_path_sets_time_created(self):
        zwo = WORKOUTS_DIR / TEMPO_FILE
        if not zwo.exists():
            self.skipTest(f"library file {TEMPO_FILE} missing")
        tc = self._time_created(
            f"/api/export/fit-workout?session_type=z2&duration_min=57&name=T&zwo_file={TEMPO_FILE}"
        )
        self.assertIsNotNone(tc, "ZWO-transcoded FIT missing file_id.time_created")
        self.assertGreater(tc, 1_577_836_800_000)


if __name__ == "__main__":
    unittest.main()
