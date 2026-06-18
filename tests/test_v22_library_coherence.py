"""v2.2 — N3 (library Option A): objective-coherence flag + honest display name.

The classifier routes by zone-dose, so an easy-labelled workout can hide a hard
set ("Endurance 120min — Z2" that secretly carries a 5×VO2 set) — the D2/D3
complaint. N3 adds an `objective_coherent` flag and appends the hidden stimulus
to the display name (e.g. "+VO2 set"), with NO routing-floor or .zwo changes.
A-3 (don't schedule incoherent files on easy slots) already exists in the
sampler's easy-slot gate (training_planner.py ~3018, same secondary flags), so
only the lying LABEL needed fixing here.
"""
import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import classify_library_content as C  # noqa: E402

CACHE = ROOT / "workouts" / ".content_classification.json"


class TestCoherenceUnit(unittest.TestCase):
    def test_easy_primary_with_hard_flag_is_incoherent(self):
        ok, suf = C.objective_coherence("endurance", {"has_vo2_work": True})
        self.assertFalse(ok)
        self.assertIn("+VO2 set", suf)
        ok, suf = C.objective_coherence("recovery", {"has_sprints": True})
        self.assertFalse(ok)
        self.assertIn("+sprints", suf)

    def test_clean_easy_primary_is_coherent(self):
        ok, suf = C.objective_coherence("endurance", {})
        self.assertTrue(ok)
        self.assertEqual(suf, [])

    def test_threshold_tolerates_vo2_ramp_but_not_sprints(self):
        self.assertTrue(C.objective_coherence("threshold", {"has_vo2_work": True})[0])
        self.assertFalse(C.objective_coherence("threshold", {"has_sprints": True})[0])

    def test_hard_primaries_always_coherent(self):
        for p in ("vo2max", "anaerobic", "neuromuscular", "over_under"):
            self.assertTrue(
                C.objective_coherence(p, {"has_sprints": True, "has_vo2_work": True})[0],
                f"{p} should be coherent by design")


class TestCoherenceInCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cls = json.loads(CACHE.read_text())
        cls.cls = cls.cls.get("classifications", cls.cls)

    def test_smoking_gun_is_flagged_and_named_honestly(self):
        sg = self.cls.get("endurance_5x2min_120min.zwo")
        self.assertIsNotNone(sg, "smoking-gun fixture missing from library")
        self.assertFalse(sg.get("objective_coherent"),
                         "endurance file hiding a VO2 set must be incoherent")
        self.assertIn("+VO2 set", sg.get("display_name", ""),
                      "honest display name must surface the hidden VO2 set")

    def test_clean_long_z2_is_coherent(self):
        clean = self.cls.get("endurance_clean_210min.zwo")
        self.assertIsNotNone(clean)
        self.assertTrue(clean.get("objective_coherent"))
        self.assertNotIn("+", clean.get("display_name", ""))


if __name__ == "__main__":
    unittest.main()
