"""Tests for the content-based ZWO classifier (v4.1.2 IMPL-CLASSIFIER).

Covers:
  * Each of the 12 rules with synthesized minimal-ZWO inputs
  * Cascade order enforcement (a workout that would qualify for multiple
    rules takes the higher-priority classification)
  * Secondary flag computation
  * Confidence ramping (just-meets-dose ~0.6, well-above-dose 1.0)
  * Filename fallback path (when content cache absent)
  * Golden set regression (parametrized by file)
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# Add scripts/ for the classifier module
HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT / "src" / "scripts"))

import classify_library_content as clc


# ── Helpers to build synthetic power arrays ──────────────────────────────────


def steady(power_frac: float, duration_s: int) -> list[float]:
    return [power_frac] * duration_s


def ramp(p_lo: float, p_hi: float, duration_s: int) -> list[float]:
    if duration_s <= 0:
        return []
    return [p_lo + (p_hi - p_lo) * t / duration_s for t in range(duration_s)]


def warmup_block(duration_s: int = 600) -> list[float]:
    """Standard easy warmup spin."""
    return ramp(0.45, 0.65, duration_s)


def cooldown_block(duration_s: int = 300) -> list[float]:
    return ramp(0.55, 0.35, duration_s)


def features_for(power: list[float]) -> dict:
    return clc.extract_features(power)


def classify_for(power: list[float], tags=None) -> tuple[str, float, dict]:
    feats = features_for(power)
    return clc.classify_features(feats, tags=tags)


# ── 1: per-rule structural tests ─────────────────────────────────────────────


class TestRule01_FtpTest(unittest.TestCase):
    """Rule 1 fires for ftp_test tag OR sustained ≥18min @≥92% with low-CV
    (single block, ≤2 distinct power levels) flanked by warmup + cooldown."""

    def test_tag_override_forces_ftp_test(self):
        # Tiny power array — tag must still win.
        power = warmup_block(60) + steady(0.65, 600) + cooldown_block(60)
        primary, conf, _ = classify_for(power, tags=["ftp_test"])
        self.assertEqual(primary, "ftp_test")
        self.assertEqual(conf, 1.0)

    def test_sustained_block_fires(self):
        # 10min warmup + 20min @100% (flat, ≤2 levels) + 10min cooldown
        power = warmup_block(600) + steady(1.00, 1200) + cooldown_block(600)
        primary, _, _ = classify_for(power)
        self.assertEqual(primary, "ftp_test")

    def test_progressive_block_does_not_fire(self):
        # 20min broken into 4 distinct levels → should NOT classify as test
        power = (
            warmup_block(600)
            + steady(0.92, 600)
            + steady(0.95, 600)
            + steady(0.98, 600)
            + steady(1.00, 600)
            + cooldown_block(300)
        )
        primary, _, _ = classify_for(power)
        self.assertNotEqual(primary, "ftp_test")

    def test_anaerobic_disqualifies_ftp_test(self):
        # Even with sustained block, Z6 work disqualifies test
        power = (
            warmup_block(600)
            + steady(1.00, 1200)
            + steady(0.50, 60)
            + steady(1.30, 30)  # anaerobic spike
            + cooldown_block(300)
        )
        primary, _, _ = classify_for(power)
        self.assertNotEqual(primary, "ftp_test")


class TestRule02_Neuromuscular(unittest.TestCase):
    """Rule 2: ≥4 sprints at ≥150% FTP, each 5-30s long."""

    def test_eight_sprints_classifies_nm(self):
        power = warmup_block(600)
        for _ in range(8):
            power += steady(1.55, 10) + steady(0.50, 180)
        power += cooldown_block(300)
        primary, conf, flags = classify_for(power)
        self.assertEqual(primary, "neuromuscular")
        self.assertGreaterEqual(conf, 0.6)
        self.assertTrue(flags["has_sprints"])

    def test_three_sprints_does_not_classify_nm(self):
        # Just below the 4-sprint floor
        power = warmup_block(600)
        for _ in range(3):
            power += steady(1.55, 10) + steady(0.50, 180)
        power += steady(0.55, 600)
        power += cooldown_block(300)
        primary, _, _ = classify_for(power)
        self.assertNotEqual(primary, "neuromuscular")


class TestRule03_Vo2Short(unittest.TestCase):
    """Rule 3: ≥8 microinterval cycles (period ≤90s, on≥1.05/off≤0.75) AND
    ≥8 min cumulative ≥1.05 FTP."""

    def test_billat_30_30_pattern(self):
        # 15 cycles of 30s @1.21 / 30s @0.60 = 7.5 min on at Z6.
        # Power 1.21 is the bottom of Z6 (post-v1.0.5d boundary fix; 1.20 is
        # now correctly the top of Z5 per Coggan/ICU half-open semantics).
        power = warmup_block(600)
        for _ in range(15):
            power += steady(1.21, 30) + steady(0.60, 30)
        power += cooldown_block(300)
        primary, conf, flags = classify_for(power)
        # 15 cycles * 30s = 450s on at 1.21 (Z6), so high_intensity_s = 7.5min
        # — close to 8 min dose, but Anaerobic rule may take it first
        # depending on Z6 dose. We expect either vo2_short or anaerobic;
        # microinterval flag must be true regardless.
        self.assertIn(primary, ("vo2_short", "anaerobic"))
        self.assertTrue(flags["pattern_microinterval"])

    def test_long_billat_30_30_classifies_vo2_short(self):
        # 18 cycles to push high_intensity_s well past 8 min Z5+Z6+Z7
        power = warmup_block(600)
        for _ in range(20):
            power += steady(1.10, 30) + steady(0.60, 30)
        power += cooldown_block(300)
        primary, _, flags = classify_for(power)
        self.assertEqual(primary, "vo2_short")
        self.assertTrue(flags["pattern_microinterval"])


class TestRule04_Anaerobic(unittest.TestCase):
    """Rule 4: Z6+Z7 ≥3 min, Z5 < 8 min."""

    def test_anaerobic_dose(self):
        # 6 x 1min @1.30 = 6 min Z6 (no microinterval pattern because period > 90s)
        power = warmup_block(600)
        for _ in range(6):
            power += steady(1.30, 60) + steady(0.50, 180)
        power += cooldown_block(300)
        primary, conf, _ = classify_for(power)
        self.assertEqual(primary, "anaerobic")
        self.assertGreaterEqual(conf, 0.6)

    def test_below_dose_falls_through(self):
        # 60s Z6 only — below 3 min anaerobic floor
        power = warmup_block(600) + steady(1.25, 60) + steady(0.55, 600) + cooldown_block(300)
        primary, _, _ = classify_for(power)
        self.assertNotEqual(primary, "anaerobic")


class TestRule05_Vo2Max(unittest.TestCase):
    """Rule 5: Z5 ≥8 min cumulative."""

    def test_classic_4x4(self):
        # 4 x 4min @1.10 = 16 min Z5 — Helgerud classic
        power = warmup_block(600)
        for _ in range(4):
            power += steady(1.10, 240) + steady(0.55, 180)
        power += cooldown_block(300)
        primary, conf, flags = classify_for(power)
        self.assertEqual(primary, "vo2max")
        self.assertEqual(conf, 1.0)  # 16 min ≥ 2 × 8 min comfortable anchor
        self.assertTrue(flags["has_vo2_work"])

    def test_just_meets_dose(self):
        # 8 min @1.10 = exactly the 8-min minimum
        power = warmup_block(600) + steady(1.10, 480) + cooldown_block(300)
        primary, conf, _ = classify_for(power)
        self.assertEqual(primary, "vo2max")
        self.assertAlmostEqual(conf, 0.6, delta=0.05)


class TestRule06_OverUnder(unittest.TestCase):
    """Rule 6: ≥3 over→under transitions + ≥18 min in 85-110% band."""

    def test_hunter_allen_3x9(self):
        # 3 sets x 3 cycles of (2min @0.95 / 1min @1.07). The over segments
        # are short enough (1 min each x 9 = 9 min) that they push into Z5
        # and trigger VO2max (rule 5) BEFORE over-under (rule 6) per the
        # cascade — that's intentional per research §3.2 ("if ≥8 min cumulative
        # at >105% FTP, call it VO2-mixed"). To verify the over-under detector
        # works in isolation, we check the secondary flag instead.
        power = warmup_block(600)
        for _set in range(3):
            for _cyc in range(3):
                power += steady(0.95, 120) + steady(1.07, 60)
            power += steady(0.55, 180)
        power += cooldown_block(300)
        _, _, flags = classify_for(power)
        self.assertTrue(flags["pattern_over_under"],
                        "OU pattern should still be detected as a secondary flag "
                        "even when VO2max is the primary classification")

    def test_short_overs_classifies_over_under(self):
        # 9 cycles x 30s overs at 1.06 = 4.5 min Z5 (below 8-min VO2max
        # dose) AND 9 cycles x 2min unders at 0.95 = 18 min in band → OU wins
        # over threshold.
        power = warmup_block(600)
        for _set in range(3):
            for _cyc in range(3):
                power += steady(0.95, 120) + steady(1.06, 30)
            power += steady(0.55, 180)
        power += cooldown_block(300)
        primary, _, flags = classify_for(power)
        self.assertEqual(primary, "over_under")
        self.assertTrue(flags["pattern_over_under"])


class TestRule07_Threshold(unittest.TestCase):
    """Rule 7: Z4 upper (95-105%) ≥15 min."""

    def test_classic_2x20(self):
        # 2 x 20min @0.98 = 40 min Z4 upper
        power = warmup_block(600)
        power += steady(0.98, 1200) + steady(0.55, 300) + steady(0.98, 1200)
        power += cooldown_block(300)
        primary, conf, flags = classify_for(power)
        self.assertEqual(primary, "threshold")
        self.assertEqual(conf, 1.0)
        self.assertTrue(flags["has_threshold_work"])

    def test_lower_z4_does_not_fire(self):
        # 30 min @0.92 — all in 90-94% sweet-spot territory, NOT threshold upper
        power = warmup_block(600) + steady(0.92, 1800) + cooldown_block(300)
        primary, _, _ = classify_for(power)
        self.assertNotEqual(primary, "threshold")


class TestRule08_SweetSpot(unittest.TestCase):
    """Rule 8: 88-94% time ≥25 min AND ≥55% of (Z3+Z4) time in band."""

    def test_classic_2x18(self):
        # 2 x 18min @0.90 = 36 min in 88-94% band
        power = warmup_block(600)
        power += steady(0.90, 1080) + steady(0.55, 240) + steady(0.90, 1080)
        power += cooldown_block(300)
        primary, _, flags = classify_for(power)
        self.assertEqual(primary, "sweet_spot")
        self.assertTrue(flags["has_sweet_spot_work"])


class TestRule09_Tempo(unittest.TestCase):
    """Rule 9: Z3 (76-90%) ≥20 min."""

    def test_classic_2x15(self):
        # 2 x 15min @0.80 = 30 min Z3
        power = warmup_block(600)
        power += steady(0.80, 900) + steady(0.55, 180) + steady(0.80, 900)
        power += cooldown_block(300)
        primary, _, _ = classify_for(power)
        self.assertEqual(primary, "tempo")


class TestRule10_Endurance(unittest.TestCase):
    """Rule 10: Z2 (55-75%) ≥45 min AND total duration ≥60 min."""

    def test_long_z2(self):
        # 70 min @0.68 (Z2)
        power = warmup_block(600) + steady(0.68, 4200) + cooldown_block(300)
        primary, _, _ = classify_for(power)
        self.assertEqual(primary, "endurance")


class TestRule11_Recovery(unittest.TestCase):
    """Rule 11: Z1 ≥70% of total AND duration ≥20 min AND no sustained burst >75%."""

    def test_easy_spin(self):
        power = warmup_block(300) + steady(0.45, 1800) + cooldown_block(120)
        primary, _, _ = classify_for(power)
        self.assertEqual(primary, "recovery")


class TestRule12_Mixed(unittest.TestCase):
    """Rule 12: fallback when no qualifying dose for any single category."""

    def test_short_subdose_workout(self):
        # 30 min total: 5 min Z3 + 5 min Z4 + lots of recovery — none meets dose
        power = warmup_block(300) + steady(0.85, 300) + steady(0.55, 300) + steady(0.95, 300) + cooldown_block(300)
        primary, _, _ = classify_for(power)
        self.assertEqual(primary, "mixed")


# ── 2: cascade-order enforcement ─────────────────────────────────────────────


class TestCascadeOrder(unittest.TestCase):

    def test_vo2_dose_beats_threshold_dose(self):
        # Z5 ≥ 8 min AND Z4 upper ≥ 15 min → VO2max (rule 5) wins
        power = warmup_block(300)
        power += steady(0.98, 1200)  # 20 min Z4 upper (would qualify threshold)
        power += steady(0.55, 120)
        power += steady(1.10, 600)   # 10 min Z5 (qualifies VO2max)
        power += cooldown_block(300)
        primary, _, _ = classify_for(power)
        self.assertEqual(primary, "vo2max")

    def test_anaerobic_dose_beats_vo2_dose_when_z5_below_threshold(self):
        # Z6+Z7 ≥3 min AND Z5 below 8min → anaerobic
        power = warmup_block(300)
        power += steady(1.30, 240)  # 4 min Z6 (anaerobic dose)
        power += steady(0.55, 60)
        power += steady(1.10, 240)  # 4 min Z5 (sub-VO2 dose)
        power += cooldown_block(300)
        primary, _, _ = classify_for(power)
        self.assertEqual(primary, "anaerobic")

    def test_nm_beats_anaerobic(self):
        # ≥4 sprints at ≥150% AND Z6 ≥3min → NM wins (rule 2 before rule 4)
        power = warmup_block(300)
        for _ in range(5):
            power += steady(1.60, 10) + steady(0.55, 60)
        power += steady(1.30, 240)  # 4 min Z6 (would qualify anaerobic)
        power += cooldown_block(300)
        primary, _, _ = classify_for(power)
        self.assertEqual(primary, "neuromuscular")

    def test_threshold_beats_sweet_spot_when_z4_upper_present(self):
        # Z4 upper ≥15min AND sweet-spot band ≥25min → threshold wins
        power = warmup_block(300)
        power += steady(0.98, 1200)  # 20 min Z4 upper
        power += steady(0.55, 60)
        power += steady(0.92, 1500)  # 25 min in sweet-spot band
        power += cooldown_block(300)
        primary, _, _ = classify_for(power)
        self.assertEqual(primary, "threshold")


# ── 3: secondary flags ───────────────────────────────────────────────────────


class TestSecondaryFlags(unittest.TestCase):

    def test_has_vo2_work_flag(self):
        # 6 min @1.10 — ≥5 min Z5 minimum
        power = warmup_block(300) + steady(1.10, 360) + cooldown_block(300)
        _, _, flags = classify_for(power)
        self.assertTrue(flags["has_vo2_work"])

    def test_polarized_consistent_flag(self):
        # 80% Z1+Z2, no Z3+Z4, some Z5
        power = (
            steady(0.50, 600 * 7)  # 70 min Z1
            + steady(0.65, 600)    # 10 min Z2
            + steady(1.10, 600)    # 10 min Z5
            + cooldown_block(300)
        )
        _, _, flags = classify_for(power)
        self.assertTrue(flags["polarized_consistent"])

    def test_pyramidal_consistent_flag(self):
        # Majority Z1/Z2 + meaningful Z3+Z4 + small Z5+
        power = (
            steady(0.50, 1500)  # 25 min Z1
            + steady(0.68, 1500)  # 25 min Z2
            + steady(0.85, 600)   # 10 min Z3
            + steady(0.98, 360)   # 6 min Z4 upper
            + steady(1.10, 60)    # 1 min Z5
        )
        _, _, flags = classify_for(power)
        self.assertTrue(flags["pyramidal_consistent"])

    def test_pattern_over_under_flag(self):
        power = warmup_block(300)
        for _ in range(5):
            power += steady(0.95, 120) + steady(1.07, 60)
        power += cooldown_block(300)
        _, _, flags = classify_for(power)
        self.assertTrue(flags["pattern_over_under"])


# ── 4: confidence ramping ────────────────────────────────────────────────────


class TestConfidence(unittest.TestCase):

    def test_well_above_dose_is_confident(self):
        # 20 min Z5 (well past 8 min minimum)
        power = warmup_block(300) + steady(1.10, 1200) + cooldown_block(300)
        primary, conf, _ = classify_for(power)
        self.assertEqual(primary, "vo2max")
        self.assertEqual(conf, 1.0)

    def test_just_meets_dose_low_confidence(self):
        # Exactly 8 min Z5
        power = warmup_block(300) + steady(1.10, 480) + cooldown_block(300)
        primary, conf, _ = classify_for(power)
        self.assertEqual(primary, "vo2max")
        self.assertAlmostEqual(conf, 0.6, delta=0.05)

    def test_below_floor_falls_back(self):
        # 5 min Z5 — below dose
        power = warmup_block(300) + steady(1.10, 300) + cooldown_block(300)
        primary, _, _ = classify_for(power)
        self.assertNotEqual(primary, "vo2max")


# ── 5: protocol enum mapping ─────────────────────────────────────────────────


class TestProtocolMapping(unittest.TestCase):

    def test_all_primary_types_mapped(self):
        from training_planner import _CONTENT_TO_PROTOCOL
        for p in clc.PRIMARY_TYPES:
            self.assertIn(p, _CONTENT_TO_PROTOCOL,
                          f"primary type {p!r} missing from _CONTENT_TO_PROTOCOL")


# ── 6: golden-set regression ─────────────────────────────────────────────────


def _load_golden_set():
    p = REPO_ROOT / "src" / "workouts" / ".golden_set.json"
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def _load_classifications():
    p = REPO_ROOT / "src" / "workouts" / ".content_classification.json"
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as f:
        return json.load(f).get("classifications", {})


class TestGoldenSetRegression(unittest.TestCase):
    """Run the full golden set as a regression suite. Skips gracefully when
    the cache or golden set is absent (e.g. CI without the workouts dir)."""

    @classmethod
    def setUpClass(cls):
        cls.golden = _load_golden_set()
        cls.classifications = _load_classifications()

    def test_overall_accuracy_meets_gate(self):
        if not self.golden or not self.classifications:
            self.skipTest("golden set or classification cache missing")
        n_correct = 0
        for entry in self.golden:
            got = self.classifications.get(entry["file"], {}).get("primary")
            if got == entry["expected_primary"]:
                n_correct += 1
        accuracy = n_correct / len(self.golden)
        self.assertGreaterEqual(accuracy, 0.90,
                                f"golden-set accuracy {accuracy:.1%} fell "
                                f"below 90% gate ({n_correct}/{len(self.golden)})")


# ── 7: filename fallback path ────────────────────────────────────────────────


class TestFilenameFallback(unittest.TestCase):
    """When the content cache is empty, _classify_protocol falls back to the
    filename-prefix heuristic. v4.1.1 FIX-PLANNER A's 7 prefix rules must
    still work in the fallback path."""

    def setUp(self):
        import training_planner as tp
        self._saved = tp._CONTENT_CLASSIFICATION_CACHE
        # 3.3.1: cache is dir-keyed now — force "loaded, empty" for the
        # CURRENT dir so _classify_protocol exercises the filename fallback.
        tp._CONTENT_CLASSIFICATION_CACHE = {str(tp.WORKOUT_DIR): {}}

    def tearDown(self):
        import training_planner as tp
        tp._CONTENT_CLASSIFICATION_CACHE = self._saved

    def test_vo2max_prefix_falls_through_to_VO2max(self):
        from training_planner import _classify_protocol
        self.assertEqual(
            _classify_protocol(0, 0, 0, 0, 600, 0, 1.2, "vo2max_billat_70min_v999.zwo"),
            "VO2max",
        )

    def test_unknown_filename_uses_dominant_zone(self):
        from training_planner import _classify_protocol
        # No prefix match, dominant zone Z2 → Endurance
        self.assertEqual(
            _classify_protocol(0, 600, 0, 0, 0, 0, 0.7, "completely_unknown_file.zwo"),
            "Endurance",
        )


if __name__ == "__main__":
    unittest.main()
