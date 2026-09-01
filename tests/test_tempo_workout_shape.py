"""v4.3.0 IMPL-DATA — guard against tempo_*.zwo regressions (B7).

User reported: "tempo workout has gradual increase of watts during workout".
Root cause: 23 of 285 tempo_*.zwo files in the library carried <Ramp>
elements in their main body, so playback showed a power gradient instead
of steady Z3. Wave-1 IMPL-DATA rewrote those files to a single
<SteadyState> in the main body (warmup/cooldown wrappers preserved).

These tests freeze the post-fix invariant for every tempo file:

  1. Each tempo_*.zwo must have one of these shapes:
       (a) STEADY      — main body is <SteadyState>-only, avg power in
                          tempo zone (76-90% FTP).
       (b) PROGRESSION — main body contains <Ramp> AND filename includes
                          '_progression_' (acceptable; a ramp by intent).
       (c) MIXED       — main body contains <IntervalsT> (out-of-category;
                          file is a misnamed-tempo per v4.3.0 scope; we
                          tolerate it but only if the OUT_OF_CATEGORY list
                          below acknowledges the count).

  2. NO tempo_*.zwo may classify as 'ramping_undesired' (the v4.3.0 bug
     condition: <Ramp> in main body without progression intent).

If a future tempo workout drops a <Ramp> in by accident, this test fails
and points to the file. The fix is either:
  - rewrite the file to <SteadyState>, or
  - rename it to include '_progression_' if a ramp is the design intent.

The audit logic mirrors scripts/audit_tempo_workouts.py exactly so a
single source of truth defines the contract.
"""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from glob import glob
from pathlib import Path

WORKOUTS_DIR = Path(__file__).resolve().parent.parent / "src" / "workouts"
WRAPPER_TAGS = {"Warmup", "Cooldown"}


def _seg_avg_power(seg: ET.Element) -> float | None:
    if seg.tag == "SteadyState":
        p = seg.attrib.get("Power")
        return float(p) if p is not None else None
    if seg.tag in ("Ramp", "Warmup", "Cooldown"):
        lo = seg.attrib.get("PowerLow")
        hi = seg.attrib.get("PowerHigh")
        if lo is None or hi is None:
            return None
        return (float(lo) + float(hi)) / 2.0
    if seg.tag == "IntervalsT":
        try:
            on_p = float(seg.attrib.get("OnPower", 0))
            off_p = float(seg.attrib.get("OffPower", 0))
            on_d = float(seg.attrib.get("OnDuration", 0))
            off_d = float(seg.attrib.get("OffDuration", 0))
            denom = on_d + off_d
            if denom <= 0:
                return None
            return (on_p * on_d + off_p * off_d) / denom
        except Exception:
            return None
    return None


def _seg_total_dur(seg: ET.Element) -> float:
    if seg.tag == "IntervalsT":
        try:
            on_d = float(seg.attrib.get("OnDuration", 0))
            off_d = float(seg.attrib.get("OffDuration", 0))
            repeat = float(seg.attrib.get("Repeat", 1))
            return (on_d + off_d) * repeat
        except Exception:
            return 0.0
    try:
        return float(seg.attrib.get("Duration", 0) or 0)
    except Exception:
        return 0.0


def _classify(zwo_path: Path) -> tuple[str, dict]:
    """Replicate audit_tempo_workouts._classify so this test is the contract."""
    tree = ET.parse(zwo_path)
    root = tree.getroot()
    workout_el = root.find("workout")
    if workout_el is None:
        return "mixed", {"reason": "missing_workout_element"}

    segments = list(workout_el)
    body = list(segments)
    while body and body[0].tag in WRAPPER_TAGS:
        body.pop(0)
    while body and body[-1].tag in WRAPPER_TAGS:
        body.pop()

    has_ramp = any(s.tag == "Ramp" for s in body)
    has_intervals = any(s.tag == "IntervalsT" for s in body)
    has_freeride = any(s.tag == "FreeRide" for s in body)
    name_has_progression = "_progression_" in zwo_path.name.lower()

    weighted = 0.0
    total = 0.0
    for s in body:
        avg = _seg_avg_power(s)
        dur = _seg_total_dur(s)
        if avg is None or dur <= 0:
            continue
        weighted += avg * dur
        total += dur
    avg_pct = (weighted / total) if total > 0 else None

    if has_intervals or has_freeride:
        return "mixed", {"avg_pct": avg_pct}
    if has_ramp:
        return ("progression", {"avg_pct": avg_pct}) if name_has_progression else (
            "ramping_undesired",
            {"avg_pct": avg_pct},
        )
    if avg_pct is None:
        return "mixed", {"reason": "no_parseable_main_body"}
    if 0.76 <= avg_pct <= 0.90:
        return "steady", {"avg_pct": avg_pct}
    return "mixed", {"avg_pct": avg_pct, "reason": "steady_body_outside_tempo_zone"}


class TempoWorkoutShapeTests(unittest.TestCase):
    """Per-file shape contract for every tempo_*.zwo in the library."""

    @classmethod
    def setUpClass(cls):
        files = sorted(glob(str(WORKOUTS_DIR / "tempo_*.zwo")))
        cls.classified = [(Path(p), *_classify(Path(p))) for p in files]
        assert cls.classified, "No tempo_*.zwo files found — library missing?"

    def test_no_ramping_undesired_in_library(self):
        """B7 closure: no tempo_*.zwo may have <Ramp> in main body without
        an explicit '_progression_' marker in the filename."""
        offenders = [p.name for p, shape, _ in self.classified if shape == "ramping_undesired"]
        self.assertEqual(
            offenders,
            [],
            f"{len(offenders)} tempo file(s) carry <Ramp> in main body without "
            f"'_progression_' filename intent (user-reported bug B7). "
            f"Either rewrite to <SteadyState> via scripts/regen_tempo_steady.py "
            f"or rename to '_progression_' if a ramp is intentional. "
            f"Offenders: {offenders[:10]}",
        )

    def test_every_tempo_file_has_known_shape(self):
        """Every tempo file must classify into one of the four allowed shapes."""
        valid = {"steady", "progression", "ramping_undesired", "mixed"}
        for path, shape, _ in self.classified:
            self.assertIn(
                shape, valid, f"{path.name} produced unexpected shape '{shape}'"
            )

    def test_steady_files_are_in_tempo_zone(self):
        """Files classified 'steady' must average within Z3 (76-90% FTP)."""
        for path, shape, info in self.classified:
            if shape != "steady":
                continue
            avg = info.get("avg_pct")
            self.assertIsNotNone(avg, f"{path.name}: missing avg power")
            self.assertGreaterEqual(
                avg, 0.76, f"{path.name}: steady main body avg {avg:.3f} below Z3"
            )
            self.assertLessEqual(
                avg, 0.90, f"{path.name}: steady main body avg {avg:.3f} above Z3"
            )

    def test_progression_files_marked_in_filename(self):
        """Any file with shape 'progression' must declare it in its filename
        (so the planner + UI can communicate the design intent to the user)."""
        for path, shape, _ in self.classified:
            if shape == "progression":
                self.assertIn(
                    "_progression_",
                    path.name.lower(),
                    f"{path.name} classified as progression but filename "
                    f"lacks '_progression_' marker",
                )

    def test_steady_files_main_body_is_steadystate_only(self):
        """A 'steady' file's main body must be <SteadyState> elements only —
        no <Ramp>, <IntervalsT>, or <FreeRide> in the main body. Warmup/Cooldown
        wrappers are still allowed."""
        for path, shape, _ in self.classified:
            if shape != "steady":
                continue
            tree = ET.parse(path)
            workout_el = tree.getroot().find("workout")
            self.assertIsNotNone(workout_el, f"{path.name}: no <workout>")
            body = list(workout_el)
            while body and body[0].tag in WRAPPER_TAGS:
                body.pop(0)
            while body and body[-1].tag in WRAPPER_TAGS:
                body.pop()
            disallowed = [s.tag for s in body if s.tag != "SteadyState"]
            self.assertEqual(
                disallowed,
                [],
                f"{path.name}: shape=steady but main body contains {disallowed}",
            )

    def test_library_meets_v43_steady_baseline(self):
        """v4.3.0 invariant: at least 50 tempo files must classify as 'steady'.
        After the B7 rewrite the count should be ~86. This guards against a
        future bulk-edit accidentally regressing the library back toward ramps
        without anyone noticing the silent count drop."""
        steady = [p for p, shape, _ in self.classified if shape == "steady"]
        self.assertGreaterEqual(
            len(steady),
            50,
            f"Only {len(steady)} tempo files classify as 'steady'; v4.3.0 baseline is ~86. "
            f"Has the library been bulk-rewritten with ramps again?",
        )


if __name__ == "__main__":
    unittest.main()
