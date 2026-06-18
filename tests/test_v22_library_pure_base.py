"""v2.2 — N2: pure single-objective library coverage.

The library had only ~49 pure-Z2 rides above 150 min (and the generator capped
endurance at 180), so gran-fondo riders lacked long pure-Z2 base rides. N2
extended generate_clean_workouts.py to 240 min and added them (classify-before-
write keeps only files that classify as endurance AND are Z2-dominant). This
guards that the long pure-Z2 base coverage exists and is genuinely pure.
"""
from pathlib import Path
import unittest

import app

WORKOUTS = Path(__file__).resolve().parents[1] / "workouts"


def _is_pure_z2(scan) -> bool:
    T = scan["total_sec"]
    if not T:
        return False
    hard = (scan["z4_sec"] + scan["z5_sec"] + scan["z6_sec"]) / T
    return ((scan["z1_sec"] + scan["z2_sec"]) / T >= 0.85
            and scan["z2_sec"] / T >= 0.55
            and scan["z3_sec"] / T < 0.12
            and hard < 0.02)


class TestLongPureZ2Coverage(unittest.TestCase):
    def test_long_pure_z2_base_rides_exist(self):
        long_pure = 0
        for f in WORKOUTS.glob("*.zwo"):
            s = app._scan_zwo_for_library(f)
            if not s or s["total_sec"] < 195 * 60:
                continue
            if _is_pure_z2(s):
                long_pure += 1
        self.assertGreaterEqual(
            long_pure, 12,
            f"expected >=12 long (>=195min) pure-Z2 base rides for gran-fondo "
            f"base, found {long_pure}")


if __name__ == "__main__":
    unittest.main()
