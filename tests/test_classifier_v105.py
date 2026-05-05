"""Tests for v1.0.5c IMPL-C — corrective fix-forward.

This release reverts the wrong Z3/Z4 boundary IMPL-V105 introduced (Coggan/
Allen + ICU standard is Z3=76-90, Z4=91-105) and adds two new pieces:

* a Sweet-Spot dominance branch in the cascade — fires when ≥25% of work
  time (or ≥10 min absolute) is in 88-94% FTP and the workout isn't
  threshold-dominated;
* a literature-grounded peak_band gate — a zone qualifies only if a single
  contiguous block lasts ≥180 s (Stöggl & Sperlich) OR ≥4 reps each ≥30 s
  cumulating ≥360 s (Billat 30/30, Rønnestad 30/15). A 60-s warmup surge
  fails both gates.

The display_name dominant-block ranker introduced by IMPL-V105 (`(repeat ×
on_s, on_power)` instead of just `repeat`) is preserved.
"""
from __future__ import annotations

import json
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


def _write_zwo(tmpdir: Path, name: str, segments_xml: str) -> Path:
    body = f"""<?xml version='1.0' encoding='UTF-8'?>
<workout_file>
  <author>v105 test</author>
  <name>{name}</name>
  <description/>
  <sportType>bike</sportType>
  <tags/>
  <workout>{segments_xml}</workout>
</workout_file>"""
    p = tmpdir / f"{name}.zwo"
    p.write_text(body, encoding="utf-8")
    return p


# ── 1: Zone-band boundaries (Coggan/Allen + ICU standard) ─────────────────────


class TestZoneBands(unittest.TestCase):
    """Z3 = 76-90% FTP, Z4 = 91-105% FTP. v1.0.5c reverts IMPL-V105's
    incorrect 87/88 boundary back to the canonical Coggan + ICU UI band."""

    def test_zone_for_088_is_z3(self):
        """88% FTP is in Z3 (top of tempo, also bottom of Sweet Spot)."""
        self.assertEqual(clc._zone_for_power(0.88), "z3")

    def test_zone_for_090_is_z3(self):
        """90% FTP is in Z3 (top of tempo)."""
        self.assertEqual(clc._zone_for_power(0.90), "z3")

    def test_zone_for_091_is_z4(self):
        """91% FTP starts Z4 (Threshold)."""
        self.assertEqual(clc._zone_for_power(0.91), "z4")

    def test_zones_ftp_dict(self):
        """ZONES_FTP source-of-truth: Z3 = [0.75, 0.91), Z4 = [0.91, 1.05).
        Half-open `[low, high)` convention so 0.90 bins to Z3 and 0.91 bins to Z4."""
        self.assertEqual(clc.ZONES_FTP["z3"], (0.75, 0.91))
        self.assertEqual(clc.ZONES_FTP["z4"], (0.91, 1.05))

    def test_zones_py_power_fracs_match(self):
        """zones.py _POWER_FRACS Z3/Z4 must agree with ICU/Coggan canonical."""
        sys.path.insert(0, str(REPO_ROOT))
        import zones  # noqa: E402
        z3_lo, z3_hi, z3_name = zones._POWER_FRACS[2]
        z4_lo, z4_hi, z4_name = zones._POWER_FRACS[3]
        self.assertEqual((z3_lo, z3_hi), (0.76, 0.90))
        self.assertEqual((z4_lo, z4_hi), (0.91, 1.05))
        self.assertIn("Z3", z3_name)
        self.assertIn("Z4", z4_name)


# ── 2: display_name dominant-block ranker (preserved from IMPL-V105) ──────────


class TestDominantBlockRanker(unittest.TestCase):
    def test_synthetic_dominant_long_block_wins(self):
        """4×60s block (240s work) must NOT outrank 2×720s block (1440s work)."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            xml = (
                '<Warmup Duration="300" PowerLow="0.5" PowerHigh="0.7" />'
                '<IntervalsT Repeat="4" OnDuration="60" OffDuration="120" '
                '            OnPower="0.98" OffPower="0.5" pace="0" />'
                '<IntervalsT Repeat="2" OnDuration="720" OffDuration="180" '
                '            OnPower="0.88" OffPower="0.5" pace="0" />'
                '<Cooldown Duration="300" PowerLow="0.7" PowerHigh="0.5" />'
            )
            p = _write_zwo(tmp, "synth_dominant_block", xml)
            res = clc.classify_zwo_v104(p)
            dn = res["display_name"]
            self.assertIn("2×12min", dn,
                          f"expected 2x12min dominant; got {dn!r}")
            self.assertIn("@ 88%", dn,
                          f"expected @ 88% (the dominant block power); got {dn!r}")

    def test_synthetic_higher_repeat_does_not_win_alone(self):
        """5×30s block must lose to 2×600s block (150s vs 1200s)."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            xml = (
                '<Warmup Duration="300" PowerLow="0.5" PowerHigh="0.7" />'
                '<IntervalsT Repeat="5" OnDuration="30" OffDuration="60" '
                '            OnPower="1.10" OffPower="0.5" pace="0" />'
                '<IntervalsT Repeat="2" OnDuration="600" OffDuration="120" '
                '            OnPower="0.95" OffPower="0.5" pace="0" />'
                '<Cooldown Duration="300" PowerLow="0.7" PowerHigh="0.5" />'
            )
            p = _write_zwo(tmp, "synth_repeat_loses", xml)
            res = clc.classify_zwo_v104(p)
            dn = res["display_name"]
            self.assertIn("2×10min", dn,
                          f"expected 2x10min dominant; got {dn!r}")
            self.assertIn("@ 95%", dn,
                          f"expected @ 95%; got {dn!r}")


# ── 3: Sweet-Spot dominance rule ──────────────────────────────────────────────


class TestSweetSpotDominance(unittest.TestCase):
    """Workouts whose primary block dwells 88-94% FTP must route to sweet_spot
    even when a brief Z5+ surge is present."""

    def test_synth_ss_with_z5_pulse_routes_to_sweet_spot(self):
        """1500 s @ 0.88 + 60 s @ 1.09 (warmup pulse) → primary=sweet_spot,
        NOT vo2max. The Z5 pulse fails the new peak_band gate."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            xml = (
                '<Warmup Duration="180" PowerLow="0.45" PowerHigh="0.6" />'
                '<SteadyState Duration="60" Power="1.09" pace="0" />'
                '<SteadyState Duration="120" Power="0.5" pace="0" />'
                '<SteadyState Duration="1500" Power="0.88" pace="0" />'
                '<Cooldown Duration="300" PowerLow="0.7" PowerHigh="0.45" />'
            )
            p = _write_zwo(tmp, "synth_ss_with_pulse", xml)
            res = clc.classify_zwo_v104(p)
            self.assertIn(
                res["primary"], ("sweet_spot", "sweet_spot_intervals"),
                f"sweet-spot dominance with 60 s Z5 pulse must route to sweet_spot; "
                f"got {res['primary']!r}",
            )

    def test_synth_threshold_workout_not_misrouted_to_sweet_spot(self):
        """5×8 min @ 0.97 → primary=threshold (NOT swept up into sweet_spot)
        — the threshold-domination guard prevents this."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            xml = (
                '<Warmup Duration="300" PowerLow="0.45" PowerHigh="0.7" />'
                '<IntervalsT Repeat="5" OnDuration="480" OffDuration="180" '
                '            OnPower="0.97" OffPower="0.55" pace="0" />'
                '<Cooldown Duration="300" PowerLow="0.7" PowerHigh="0.45" />'
            )
            p = _write_zwo(tmp, "synth_threshold_5x8", xml)
            res = clc.classify_zwo_v104(p)
            self.assertEqual(
                res["primary"], "threshold",
                f"5×8min @ 0.97 must classify as threshold, not sweet_spot; "
                f"got {res['primary']!r}",
            )


# ── 4: Canary file ────────────────────────────────────────────────────────────


class TestCanaryFile(unittest.TestCase):
    """tempo_2x12min_63min.zwo — the audit's marquee bug.

    HARD GATE: primary MUST be sweet_spot, display_name MUST mention 2×12min.
    """

    @classmethod
    def setUpClass(cls):
        if not CLASSIFICATION_PATH.exists():
            raise unittest.SkipTest(f"missing {CLASSIFICATION_PATH}")
        with CLASSIFICATION_PATH.open() as f:
            cls.cache = json.load(f)
        cls.cls_map = cls.cache.get("classifications", {})
        cls.entry = cls.cls_map.get("tempo_2x12min_63min.zwo")

    def test_canary_present(self):
        self.assertIsNotNone(self.entry, "canary file missing from cache")

    def test_canary_lands_in_sweet_spot_HARD_GATE(self):
        """HARD GATE: canary primary MUST be `sweet_spot`."""
        primary = self.entry["primary"]
        self.assertEqual(
            primary, "sweet_spot",
            f"canary primary={primary!r}; must be sweet_spot. "
            f"vo2max and tempo_intervals are both regressions.",
        )

    def test_canary_display_name_features_12min(self):
        """display_name must mention the dominant 2×12min interval block."""
        dn = self.entry["display_name"]
        self.assertTrue(
            "2×12min" in dn or "2x12min" in dn or "12min × 2" in dn,
            f"display_name does not feature the 12-minute dominant block: {dn!r}",
        )


# ── 5: peak-band sustained-presence gate ──────────────────────────────────────


class TestPeakBandSustainedGate(unittest.TestCase):
    """The peak_band feature must require sustained presence (≥180 s
    contiguous) OR a microinterval cluster (≥4 reps each ≥30 s, cumulating
    ≥360 s). A 60-s warmup surge fails both gates."""

    def _segments_for(self, total_s: int) -> list[dict]:
        return [{"kind": "interval", "duration_s": total_s}]

    def test_brief_z5_pulse_does_not_promote_peak_band(self):
        """60 s @ 1.10 + 1500 s @ 0.65 → peak_band=z2 (60 s fails BOTH gates)."""
        power = [1.10] * 60 + [0.65] * 1500
        feats = clc._peak_band_features(power, self._segments_for(len(power)))
        self.assertEqual(
            feats["peak_band"], "z2",
            f"single 60-s Z5 pulse must not promote peak_band; got {feats}",
        )

    def test_sustained_z5_block_promotes_peak_band(self):
        """3 min @ 1.10 + 30 min @ 0.65 → peak_band=z5 (sustained gate met)."""
        power = [1.10] * 180 + [0.65] * 1800
        feats = clc._peak_band_features(power, self._segments_for(len(power)))
        self.assertEqual(
            feats["peak_band"], "z5",
            f"180 s contiguous Z5 must promote peak_band; got {feats}",
        )

    def test_microinterval_cluster_promotes_peak_band(self):
        """4 × 4 min @ 1.10 (240 s each, 4 reps, 960 s total) → peak_band=z5
        (passes both gates: 240 ≥ 180 sustained AND 4 reps ≥ 4 with cum ≥ 360)."""
        power: list[float] = []
        for _ in range(4):
            power += [1.10] * 240  # 4-min Z5 rep
            power += [0.50] * 120  # 2-min recovery
        feats = clc._peak_band_features(power, self._segments_for(len(power)))
        self.assertEqual(
            feats["peak_band"], "z5",
            f"4×4min Z5 microinterval cluster must promote peak_band; got {feats}",
        )

    def test_short_microcluster_fails_gate(self):
        """3 × 60 s @ 1.10 (3 reps, 180 s cum) → peak_band stays below z5
        (sustained gate fails: longest=60 < 180; reps gate fails: 3 < 4)."""
        power: list[float] = []
        for _ in range(3):
            power += [1.10] * 60
            power += [0.50] * 120
        # Pad with z2 to make a realistic workout
        power += [0.65] * 1200
        feats = clc._peak_band_features(power, self._segments_for(len(power)))
        self.assertNotEqual(
            feats["peak_band"], "z5",
            f"3×60s Z5 cluster must NOT promote peak_band to z5; got {feats}",
        )


# ── 6: JSON integrity ─────────────────────────────────────────────────────────


class TestJSONIntegrity(unittest.TestCase):
    """The regenerated cache must still cover every workout."""

    @classmethod
    def setUpClass(cls):
        if not CLASSIFICATION_PATH.exists():
            raise unittest.SkipTest(f"missing {CLASSIFICATION_PATH}")
        with CLASSIFICATION_PATH.open() as f:
            cls.cache = json.load(f)

    def test_total_file_count_3054(self):
        self.assertEqual(self.cache.get("count"), 3054,
                         "total file count must remain 3054 post-v105c regen")
        self.assertEqual(len(self.cache.get("classifications", {})), 3054)

    def test_no_empty_display_names(self):
        empty = [
            f for f, e in self.cache["classifications"].items()
            if not (e.get("display_name") or "").strip()
        ]
        self.assertEqual(empty, [],
                         f"{len(empty)} entries have empty display_name")


if __name__ == "__main__":
    unittest.main()
