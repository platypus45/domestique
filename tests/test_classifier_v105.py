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
    incorrect 87/88 boundary back to the canonical Coggan + ICU UI band.

    v1.0.5d (BUG-A fix) extends ZONES_FTP upper bounds by +0.01 so that
    half-open `[low, high)` semantics put exact-zone-boundary values like
    0.90, 1.05, 1.20, 1.50 in their named zone (Z3, Z4, Z5, Z6), not the
    next zone up. ICU UI + Hunter Allen Power Blog confirm 105% = top of
    Z4 Threshold (not Z5 VO2max)."""

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
        """ZONES_FTP source-of-truth post v1.0.5d (BUG-A fix):
        Z3 = [0.76, 0.91), Z4 = [0.91, 1.06), Z5 = [1.06, 1.21).
        Half-open `[low, high)` — top-of-zone values 0.90/1.05/1.20/1.50 stay
        in their named zone (Z3/Z4/Z5/Z6), verified against ICU UI + Coggan."""
        self.assertEqual(clc.ZONES_FTP["z3"], (0.76, 0.91))
        self.assertEqual(clc.ZONES_FTP["z4"], (0.91, 1.06))
        self.assertEqual(clc.ZONES_FTP["z5"], (1.06, 1.21))
        self.assertEqual(clc.ZONES_FTP["z6"], (1.21, 1.51))

    def test_zones_py_power_fracs_match(self):
        """zones.py _POWER_FRACS Z3/Z4 must agree with ICU/Coggan canonical.
        zones.py uses inclusive `[low, high]` tuples so its (0.91, 1.05) for
        Z4 is correct under inclusive semantics — no v1.0.5d change there."""
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
    """tempo_4x1min-2min_98pct_66min.zwo — the audit's marquee bug.

    HARD GATE: primary MUST be sweet_spot, display_name MUST mention 2×12min.
    """

    @classmethod
    def setUpClass(cls):
        if not CLASSIFICATION_PATH.exists():
            raise unittest.SkipTest(f"missing {CLASSIFICATION_PATH}")
        with CLASSIFICATION_PATH.open() as f:
            cls.cache = json.load(f)
        cls.cls_map = cls.cache.get("classifications", {})
        cls.entry = cls.cls_map.get("tempo_4x1min-2min_98pct_66min.zwo")

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

    def test_total_file_count_consistent(self):
        # v1.10.0: the library grows over time, so assert the INVARIANT — the
        # cache "count" field tracks the classifications dict — instead of a
        # frozen snapshot. Was hardcoded 3054, stale since the library grew.
        n = len(self.cache.get("classifications", {}))
        self.assertGreater(n, 0)
        self.assertEqual(self.cache.get("count"), n,
                         "cache 'count' must equal the number of classifications")

    def test_no_empty_display_names(self):
        empty = [
            f for f, e in self.cache["classifications"].items()
            if not (e.get("display_name") or "").strip()
        ]
        self.assertEqual(empty, [],
                         f"{len(empty)} entries have empty display_name")


# ── 7: v1.0.5d half-open boundary regression (BUG-A) ──────────────────────────


class TestZoneBoundariesV105D(unittest.TestCase):
    """Half-open `[low, high)` semantics post BUG-A fix. Top-of-zone values
    (0.90, 1.05, 1.20, 1.50) stay in their named zone — they no longer drift
    one zone up under the previous off-by-one upper bounds."""

    def test_zone_for_top_of_z3_stays_z3(self):
        """0.90 (top of Z3 per Coggan/ICU) → Z3."""
        self.assertEqual(clc._zone_for_power(0.90), "z3")

    def test_zone_for_top_of_z4_stays_z4(self):
        """1.05 (top of Z4 Threshold per Coggan/ICU) → Z4. BUG-A regression
        guard — was binning to Z5 under the previous upper bound 1.05."""
        self.assertEqual(clc._zone_for_power(1.05), "z4")

    def test_zone_for_top_of_z5_stays_z5(self):
        """1.20 (top of Z5 VO2max) → Z5."""
        self.assertEqual(clc._zone_for_power(1.20), "z5")

    def test_zone_for_bottom_of_z4(self):
        """0.91 (bottom of Z4 Threshold) → Z4."""
        self.assertEqual(clc._zone_for_power(0.91), "z4")

    def test_zone_for_bottom_of_z5(self):
        """1.06 (bottom of Z5 VO2max) → Z5."""
        self.assertEqual(clc._zone_for_power(1.06), "z5")


# ── 8: v1.0.5d confirmed-bug regression files (cache-driven) ──────────────────


class TestConfirmedBugsV105D(unittest.TestCase):
    """All 8 confirmed classifier bugs from /tmp/qa_v105_validation.md must
    resolve to non-buggy classes in the regenerated cache."""

    @classmethod
    def setUpClass(cls):
        if not CLASSIFICATION_PATH.exists():
            raise unittest.SkipTest(f"missing {CLASSIFICATION_PATH}")
        with CLASSIFICATION_PATH.open() as f:
            cache = json.load(f)
        cls.cls_map = cache.get("classifications", {})

    def _primary(self, fname: str) -> str:
        entry = self.cls_map.get(fname)
        self.assertIsNotNone(entry, f"{fname} missing from classification cache")
        return entry["primary"]

    # --- BUG-A: 105% FTP top-of-Z4 was binning to Z5 → vo2max instead of threshold

    def test_bug_a_vo2max_2min_7x_56min_now_threshold(self):
        """threshold_7x90s-3min_105pct_56min.zwo → threshold (BUG-A; was vo2max)."""
        self.assertEqual(self._primary("threshold_7x90s-3min_105pct_56min.zwo"), "threshold")

    def test_bug_a_vo2max_mixed_40min_now_threshold(self):
        """threshold_3x5min-5min_105pct_40min_v2.zwo → threshold (BUG-A; was vo2max)."""
        self.assertEqual(self._primary("threshold_3x5min-5min_105pct_40min_v2.zwo"), "threshold")

    def test_bug_a_vo2max_mixed_60min_now_threshold(self):
        """threshold_2x5min-90s_105pct_60min.zwo → threshold (BUG-A; was vo2max)."""
        self.assertEqual(self._primary("threshold_2x5min-90s_105pct_60min.zwo"), "threshold")

    def test_bug_a_vo2max_10x2min_70min_now_threshold(self):
        """threshold_2x5x2min-1min_95pct_75min.zwo → threshold (BUG-A; was vo2max)."""
        self.assertEqual(self._primary("threshold_2x5x2min-1min_95pct_75min.zwo"), "threshold")

    # --- BUG-B: z6 ≥60s floor in z1-dom fallback → mis-routing endurance to anaerobic

    def test_bug_b_billat_30_30_not_anaerobic(self):
        """vo2max_2x30s-30s_120pct_33min_v2.zwo → NOT anaerobic (BUG-B). Billat
        30/30 microintervals have brief Z6 surges that were tripping the 60-s
        z6 floor; raised to 180 s (3 min Coggan/FasCat anaerobic minimum). Per
        QA-V105 the right destination is one of vo2_short (microinterval
        pattern), vo2max (Z5 dose) or endurance (Z2-dominant majority) — the
        v1.0.5d boundary fix exposed Z5 dose that previously binned to Z6."""
        self.assertNotEqual(
            self._primary("vo2max_2x30s-30s_120pct_33min_v2.zwo"), "anaerobic",
        )

    def test_bug_b_tempo_steady_45min_v2_not_anaerobic(self):
        """tempo_ladder5_120pct_49min.zwo → NOT anaerobic (BUG-B)."""
        self.assertNotEqual(
            self._primary("tempo_ladder5_120pct_49min.zwo"), "anaerobic",
        )

    # --- BUG-C: OU detector under-leg lower bound 0.70 caught Z3 ramps

    def test_bug_c_anaerobic_2x1min_64min_not_over_under(self):
        """anaerobic_6x40s_125pct_71min.zwo → anaerobic or vo2_short (BUG-C; was
        over_under). Z3 ramp before/after Z6 sprint was satisfying alternation
        count under the old 0.70 under-leg lower bound."""
        self.assertIn(
            self._primary("anaerobic_6x40s_125pct_71min.zwo"),
            ("anaerobic", "vo2_short"),
        )

    def test_bug_c_anaerobic_4x20s_56min_not_over_under(self):
        """anaerobic_4x5min_76pct_59min.zwo → vo2_ladder, anaerobic, or neuromuscular
        (BUG-C; was over_under)."""
        self.assertIn(
            self._primary("anaerobic_4x5min_76pct_59min.zwo"),
            ("vo2_ladder", "anaerobic", "neuromuscular"),
        )


if __name__ == "__main__":
    unittest.main()
