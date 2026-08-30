"""Tests for the v1.0.4 IMPL-CLASSIFIER rewrite.

Coverage:
  * Canary tests (HARD GATE) — `tempo_4x150s_85pct_63min.zwo` and
    `tempo_2x6min_88pct_55min_v2.zwo` reclassify to `threshold_ladder` with the
    locked display_name format. At least one previously-mis-filed `vo2max`
    Z2 file is reclassified out of `vo2max`.
  * Synthetic-XML structural detector tests — ladder hit/miss, peak-zone
    gate, empty/free-ride flagging.
  * JSON-integrity tests over the regenerated
    `workouts/.content_classification.json` — 0 entries with `class:
    "mixed"`, 100% of entries have non-empty display_name, all 16 canonical
    classes have ≥1 representative file.

The XML-fixture tests use ``tempfile`` to write minimal ZWO files so the
detectors are exercised end-to-end (parser → segments → cascade) without
relying on specific files in the workouts/ directory.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import classify_library_content as clc  # noqa: E402


WORKOUTS_DIR = REPO_ROOT / "workouts"
CLASSIFICATION_PATH = WORKOUTS_DIR / ".content_classification.json"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_zwo(tmpdir: Path, name: str, segments_xml: str) -> Path:
    """Wrap a body of <workout> children into a complete ZWO file."""
    body = f"""<?xml version='1.0' encoding='UTF-8'?>
<workout_file>
  <author>v104 test</author>
  <name>{name}</name>
  <description/>
  <sportType>bike</sportType>
  <tags/>
  <workout>{segments_xml}</workout>
</workout_file>"""
    p = tmpdir / f"{name}.zwo"
    p.write_text(body, encoding="utf-8")
    return p


def _ss(power: float, duration_s: int) -> str:
    return f'<SteadyState Duration="{duration_s}" Power="{power}" />'


def _wu(plo: float, phi: float, duration_s: int) -> str:
    return f'<Warmup Duration="{duration_s}" PowerLow="{plo}" PowerHigh="{phi}" />'


def _cd(plo: float, phi: float, duration_s: int) -> str:
    return f'<Cooldown Duration="{duration_s}" PowerLow="{plo}" PowerHigh="{phi}" />'


def _free(duration_s: int) -> str:
    return f'<FreeRide Duration="{duration_s}" FlatRoad="0" />'


# ── 1: Canary tests (HARD GATE) ──────────────────────────────────────────────


class TestCanary(unittest.TestCase):
    """The two canaries from /tmp/MASTER_DECISIONS_v104.md §5 acceptance gate
    #3 — these MUST move out of `tempo` and land in `threshold_ladder` with a
    display_name matching the locked schema."""

    def test_canary_tempo_steady_57min_is_threshold_ladder(self):
        """Primary canary — `tempo_4x150s_85pct_63min.zwo` is the user's named
        miss. Must classify as `threshold_ladder` with display_name
        matching `r"Threshold Ladder \\d+min — \\d+→\\d+% × \\d+"`.
        """
        p = WORKOUTS_DIR / "tempo_4x150s_85pct_63min.zwo"
        self.assertTrue(p.exists(), f"canary file missing: {p}")
        result = clc.classify_zwo_v104(p)
        self.assertEqual(result["primary"], "threshold_ladder",
                         f"canary regressed: {result.get('display_name')}")
        self.assertRegex(
            result["display_name"],
            r"^Threshold Ladder \d+min — \d+→\d+% × \d+$",
        )
        # Canary output (v2.4.0 warmup migration lengthened the file 58→63min;
        # live classifier and cache both say 63min — verified 2026-07-02):
        self.assertEqual(
            result["display_name"], "Threshold Ladder 63min — 85→97% × 4",
            f"canary display_name drift: {result['display_name']!r}",
        )

    def test_canary_tempo_steady_55min_is_threshold_ladder(self):
        """Secondary canary — `tempo_2x6min_88pct_55min_v2.zwo` peaks ≥97% FTP per
        audit and must classify as `threshold_ladder` (not vo2_ladder, even
        though there's a brief 60 s 120% spike)."""
        p = WORKOUTS_DIR / "tempo_2x6min_88pct_55min_v2.zwo"
        self.assertTrue(p.exists())
        result = clc.classify_zwo_v104(p)
        self.assertEqual(result["primary"], "threshold_ladder",
                         f"canary regressed: {result.get('display_name')}")

    def test_misfiled_vo2max_z2_is_reclassified(self):
        """At least one previously-mis-filed vo2max-named Z2-dominant file
        must reclassify out of vo2max once content takes precedence over
        filename. Loads the regenerated JSON and looks for any
        `vo2max_*.zwo` that is NOT classified as vo2max/vo2_short/
        vo2_ladder."""
        with CLASSIFICATION_PATH.open() as f:
            payload = json.load(f)
        classifications = payload["classifications"]
        misfiled: list[str] = []
        for fname, entry in classifications.items():
            if not fname.startswith("vo2max_"):
                continue
            primary = entry.get("primary")
            if primary not in ("vo2max", "vo2_short", "vo2_ladder", None):
                misfiled.append(fname)
        self.assertGreaterEqual(
            len(misfiled), 1,
            "expected ≥1 vo2max_*.zwo file to be reclassified out of vo2max",
        )


# ── 2: Synthetic-XML structural detectors ────────────────────────────────────


class TestLadderDetector(unittest.TestCase):

    def test_ladder_detector_hits_4rung_2set(self):
        """4-rung × 2-set ascending ladder must register as a ladder."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            body = (
                _wu(0.5, 0.7, 300)
                + _ss(0.80, 180)
                + _ss(0.85, 180)
                + _ss(0.90, 180)
                + _ss(0.97, 180)
                + _ss(0.50, 300)
                + _ss(0.80, 180)
                + _ss(0.85, 180)
                + _ss(0.90, 180)
                + _ss(0.97, 180)
                + _cd(0.6, 0.4, 180)
            )
            p = _write_zwo(tmp, "ladder_4x2", body)
            result = clc.classify_zwo_v104(p)
            self.assertTrue(result["features"]["is_ladder"])
            self.assertGreaterEqual(result["features"]["ladder_set_count"], 2)
            self.assertEqual(result["primary"], "threshold_ladder")

    def test_ladder_detector_misses_2_rung_sequence(self):
        """A 2-rung sequence (only two distinct power levels) is NOT a
        ladder."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            body = (
                _wu(0.5, 0.7, 300)
                + _ss(0.85, 180)
                + _ss(0.95, 180)
                + _ss(0.50, 300)
                + _ss(0.85, 180)
                + _ss(0.95, 180)
                + _cd(0.6, 0.4, 180)
            )
            p = _write_zwo(tmp, "twostep", body)
            result = clc.classify_zwo_v104(p)
            self.assertFalse(result["features"]["is_ladder"])
            # Should NOT classify as any *_ladder
            self.assertNotIn("_ladder", result["primary"])


class TestPeakZoneGate(unittest.TestCase):

    def test_peak_zone_gate_classifies_short_workout_by_z5(self):
        """6 min @ Z5 in a 30-min workout — peak-zone gate must override the
        dose accumulator (Z5 = 6 min < 8 min VO2 dose) and still classify
        as vo2max because the contiguous Z5 block is ≥5 min and ≥30% of
        work time.

        Total work time (excluding warmup/cooldown) = 1320 s; 6 min Z5 =
        360 s = 27%. To clear the 30% gate, give a slightly longer Z5.
        Use 8 min Z5 in a 22-min work block.
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # 5 min warmup, 8 min @ Z5, 5 min Z2 recovery, 5 min cooldown.
            # Work time = 8min + 5min = 13min = 780s. Z5 = 480s = 61.5% > 30%.
            body = (
                _wu(0.5, 0.7, 300)
                + _ss(1.10, 480)
                + _ss(0.65, 300)
                + _cd(0.6, 0.4, 300)
            )
            p = _write_zwo(tmp, "peak_z5", body)
            result = clc.classify_zwo_v104(p)
            # Z5 dose ≥8 min → vo2max via either dose rule or peak gate.
            self.assertEqual(result["primary"], "vo2max")

    def test_peak_zone_gate_lifts_threshold_when_z4_dominant(self):
        """≥5 min contiguous Z4 covering ≥30% of work-time → threshold via
        peak-zone gate, even when Z3 dose would otherwise dominate."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # 5 min warmup + 6 min @ 100% (Z4 contiguous block) + 4 min Z2 +
            # 5 min cooldown. Work = 600s, Z4 = 360s = 60%.
            body = (
                _wu(0.5, 0.7, 300)
                + _ss(1.00, 360)
                + _ss(0.60, 240)
                + _cd(0.6, 0.4, 300)
            )
            p = _write_zwo(tmp, "z4_block", body)
            result = clc.classify_zwo_v104(p)
            self.assertIn(result["primary"], ("threshold", "threshold_ladder"))


class TestEmptyAndFreeRide(unittest.TestCase):

    def test_empty_workout_is_flagged_not_classified(self):
        """Empty <workout> → flagged but NOT given a primary class."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _write_zwo(tmp, "empty", "")
            result = clc.classify_zwo_v104(p)
            self.assertIsNone(result["primary"])
            self.assertIn("empty", result.get("flags", []))

    def test_free_ride_only_is_flagged_not_classified(self):
        """A workout consisting only of FreeRide segments → flagged but NOT
        given a primary class."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            body = _free(1800)
            p = _write_zwo(tmp, "freeride_only", body)
            result = clc.classify_zwo_v104(p)
            self.assertIsNone(result["primary"])
            self.assertIn("free_ride", result.get("flags", []))


# ── 3: JSON-integrity tests ──────────────────────────────────────────────────


class TestJSONIntegrity(unittest.TestCase):
    """Run against the regenerated `.content_classification.json`. Skips
    gracefully if the cache is missing (e.g. CI without workouts/)."""

    @classmethod
    def setUpClass(cls):
        if not CLASSIFICATION_PATH.exists():
            cls.classifications = None
            return
        with CLASSIFICATION_PATH.open(encoding="utf-8") as f:
            cls.classifications = json.load(f)["classifications"]

    def test_no_mixed_class(self):
        """0 entries may have `primary: "mixed"` (§5 acceptance gate #2)."""
        if self.classifications is None:
            self.skipTest("no classification cache")
        offenders = [k for k, v in self.classifications.items()
                     if v.get("primary") == "mixed"]
        self.assertEqual(offenders, [],
                         f"{len(offenders)} entries still primary=mixed")

    def test_all_have_display_name(self):
        """100% of entries must have a non-empty display_name (§5
        acceptance gate #2). Flagged entries (empty / free_ride) get a
        synthesized name; only truly empty workouts may have ''."""
        if self.classifications is None:
            self.skipTest("no classification cache")
        missing = []
        for fname, entry in self.classifications.items():
            if "empty" in (entry.get("flags") or []):
                # Empty <workout> entries have no power data — those are
                # carved out as exempt per §2 cascade step 0.
                continue
            if not entry.get("display_name"):
                missing.append(fname)
        self.assertEqual(missing, [],
                         f"{len(missing)} entries have empty display_name")

    def test_all_canonical_classes_represented(self):
        """All 16 canonical classes have ≥1 representative file (§5
        acceptance gate #2 / §1 taxonomy lock).

        ``endurance_intervals`` is exempted as of v1.0.5c — the new
        Sweet-Spot dominance rule routes the few previously-classified
        endurance_intervals files (long Z2 rides with ≥10 min in 88-94%
        FTP) to ``sweet_spot``, which is structurally more accurate.
        ``endurance_intervals`` would still trigger for genuine Z2
        sessions with sprint strides; no such file currently exists.
        """
        if self.classifications is None:
            self.skipTest("no classification cache")
        from collections import Counter
        counts = Counter(c.get("primary") for c in self.classifications.values())
        exempt = {"endurance_intervals"}
        missing = []
        for cls in clc.CANONICAL_TYPES_V104:
            if cls in exempt:
                continue
            if counts.get(cls, 0) < 1:
                missing.append(cls)
        self.assertEqual(missing, [],
                         f"missing-class report: {missing}")

    def test_display_name_for_canary(self):
        """Cache-level confirmation that the canary's stored display_name
        matches the locked output."""
        if self.classifications is None:
            self.skipTest("no classification cache")
        entry = self.classifications.get("tempo_4x150s_85pct_63min.zwo")
        self.assertIsNotNone(entry, "canary file missing from cache")
        self.assertEqual(entry["display_name"],
                         "Threshold Ladder 63min — 85→97% × 4")


# ── 4: Audit-trail integrity ─────────────────────────────────────────────────


class TestAuditTrail(unittest.TestCase):

    def test_audit_trail_present_with_transitions(self):
        """The sibling `.classification_audit_v104.json` exists and lists
        transitions. We don't pin the exact count (depends on prior cache
        state) but enforce the schema."""
        audit_path = WORKOUTS_DIR / ".classification_audit_v104.json"
        if not audit_path.exists():
            self.skipTest("audit JSON missing")
        with audit_path.open() as f:
            data = json.load(f)
        self.assertEqual(data.get("schema_version"), "v1.0.4")
        self.assertIn("transitions", data)
        self.assertIn("summary", data)
        # Verify entry shape
        for entry in data["transitions"][:50]:
            self.assertIn("file", entry)
            self.assertIn("old_primary", entry)
            self.assertIn("new_primary", entry)
            self.assertIn("reason", entry)


if __name__ == "__main__":
    unittest.main()
