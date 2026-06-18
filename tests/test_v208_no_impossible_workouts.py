"""v2.0.8 — D1: impossible workouts shipped in the library (the user's "45min @ Z7"
— actually anaerobic_ramp_* staircases climbing to 600% FTP in 120-150s SteadyState
steps). They got through because the dangerous-workout screen blanket-exempted ANY
filename containing "ramp" (amend_unsafe_workouts.py). Fix: deleted the 2 garbage
files + tightened the exemption to genuine TESTS (ftp_test / ramp_test) only.

This invariant guards against any impossible sustained-supramaximal workout —
present OR newly introduced by a future library regen.
"""
import glob
import re
import unittest
from pathlib import Path

WORKOUTS = Path(__file__).resolve().parent.parent / "workouts"


def _max_sustained_supra_seconds(txt, ftp_frac=1.5):
    """Longest single SteadyState segment held above ftp_frac×FTP, in seconds."""
    worst = 0.0
    for m in re.finditer(r'<SteadyState[^>]*Duration="([\d.]+)"[^>]*Power="([\d.]+)"', txt):
        d, p = float(m.group(1)), float(m.group(2))
        if p > ftp_frac:
            worst = max(worst, d)
    for m in re.finditer(r'<SteadyState[^>]*Power="([\d.]+)"[^>]*Duration="([\d.]+)"', txt):
        p, d = float(m.group(1)), float(m.group(2))
        if p > ftp_frac:
            worst = max(worst, d)
    return worst


class TestNoImpossibleWorkouts(unittest.TestCase):
    def test_no_sustained_supramaximal_block_over_90s(self):
        # Nobody can hold >150% FTP as a single steady block for >90s — such a
        # segment means a corrupt/impossible file (the anaerobic_ramp 600% staircase).
        bad = []
        for f in glob.glob(str(WORKOUTS / "*.zwo")):
            worst = _max_sustained_supra_seconds(Path(f).read_text())
            if worst > 90:
                bad.append((Path(f).name, int(worst)))
        self.assertEqual(bad, [], f"impossible sustained-Z7 workouts present: {bad}")

    def test_broken_anaerobic_ramp_files_are_gone(self):
        for f in ("anaerobic_ramp_58min.zwo", "anaerobic_ramp_38min.zwo"):
            self.assertFalse((WORKOUTS / f).exists(), f"{f} must be deleted (impossible)")

    def test_no_whole_workout_if_over_1_05_except_tests(self):
        # D2 guard: a workout whose WHOLE-workout IF > 1.05 implies sustained
        # supra-FTP average — impossible (the user's "47min = 179 TSS"). Only
        # genuine ramp/FTP TESTS (which ramp to max) legitimately exceed this.
        import json
        idx = json.loads((WORKOUTS / ".library_index.json").read_text())
        rows = idx.get("workouts") or (idx if isinstance(idx, list) else [])
        bad = []
        for w in rows:
            f = (w.get("File") or "")
            if "ftp_test" in f or "ramp_test" in f:
                continue
            tss = float(w.get("TSS") or 0)
            dur = float(w.get("Duration(min)") or 0)
            if dur > 0 and tss > 0:
                iff = (tss / (dur / 60) / 100) ** 0.5
                if iff > 1.05:
                    bad.append((f, round(iff, 2)))
        self.assertEqual(bad, [], f"impossible whole-workout IF (>1.05): {bad}")


if __name__ == "__main__":
    unittest.main()
