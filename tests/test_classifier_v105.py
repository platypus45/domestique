"""Tests for the v1.0.5 IMPL — zone-band Z3/Z4 boundary fix
(Allen-Coggan: 88% FTP belongs in Z4, not Z3) + display_name dominant-block
ranker (`(repeat × on_s, on_power)` instead of just `repeat`).

Cascade-router behaviour beyond zone binning is NOT in scope for v1.0.5; tests
here only protect the boundary literal and the display-name ranker.
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


# ── 1: Zone-band boundary fix (Allen-Coggan 88% Z3↔Z4) ────────────────────────


class TestZoneBands(unittest.TestCase):
    def test_zone_for_088_is_z4(self):
        """88% FTP must bin to Z4 (Allen-Coggan threshold lower bound)."""
        self.assertEqual(clc._zone_for_power(0.88), "z4")

    def test_zone_for_087_is_z3(self):
        """87% FTP must remain in Z3 (the boundary preserved)."""
        self.assertEqual(clc._zone_for_power(0.87), "z3")

    def test_zones_ftp_dict(self):
        """ZONES_FTP source-of-truth: Z3 top is 0.88, Z4 bottom is 0.88."""
        self.assertEqual(clc.ZONES_FTP["z3"], (0.75, 0.88))
        self.assertEqual(clc.ZONES_FTP["z4"], (0.88, 1.05))

    def test_zones_py_power_fracs_match(self):
        """zones.py _POWER_FRACS Z3/Z4 must agree with ZONES_FTP."""
        sys.path.insert(0, str(REPO_ROOT))
        import zones  # noqa: E402
        z3_lo, z3_hi, z3_name = zones._POWER_FRACS[2]
        z4_lo, z4_hi, z4_name = zones._POWER_FRACS[3]
        self.assertEqual((z3_lo, z3_hi), (0.76, 0.87))
        self.assertEqual((z4_lo, z4_hi), (0.88, 1.05))
        self.assertIn("Z3", z3_name)
        self.assertIn("Z4", z4_name)


# ── 2: display_name dominant-block ranker ────────────────────────────────────


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


# ── 3: Canary file + JSON integrity ──────────────────────────────────────────


class TestCanaryFile(unittest.TestCase):
    """tempo_2x12min_63min.zwo — the audit's marquee bug.

    After v1.0.5: the file MUST NOT classify as `tempo_intervals` (the v1.0.4
    bug), and the display_name MUST feature the dominant 2×12min block.
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

    def test_canary_not_tempo_intervals(self):
        """The marquee regression: 88% block must not bin to Z3 → tempo_intervals."""
        self.assertNotEqual(self.entry["primary"], "tempo_intervals",
                            f"canary regressed to tempo_intervals: {self.entry}")

    def test_canary_display_name_features_12min(self):
        """display_name must mention the dominant 2×12min interval block."""
        dn = self.entry["display_name"]
        self.assertTrue(
            "2×12min" in dn or "2x12min" in dn or "12min × 2" in dn,
            f"display_name does not feature the 12-minute dominant block: {dn!r}",
        )


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
                         "total file count must remain 3054 post-v105 regen")
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
