"""The generator must deliver exactly what it claims, and claim only what a
published protocol supports.

The whole point of generating rather than searching is that the dose becomes an
input instead of an outcome. That is only true if the arithmetic inside the
generator matches the file it writes -- so the load-bearing test here is a ROUND
TRIP: generate, then measure the emitted .zwo with the same scanner the library
uses, and require agreement.

That test earned itself immediately. The first version assumed an IntervalsT
block emits (reps - 1) recovery legs; it emits `reps`. The accounting came out
4 minutes under what the scanner measured on the file the same function had
just written, and no amount of reading the code would have shown it.
"""
import os
import pathlib
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

os.environ.setdefault("PYTHONHASHSEED", "0")
SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
import workout_gen as wg  # noqa: E402

# (session_type, window_min, dose_min) -- spread across protocols and windows.
REQUESTS = [
    ("vo2max", 60, 18), ("vo2max", 45, 12), ("vo2max", 90, 30), ("vo2max", 75, 20),
    ("threshold", 75, 32), ("threshold", 60, 20), ("threshold", 100, 40),
    ("overunder", 70, 40), ("overunder", 90, 30),
    ("sweetspot", 90, 60), ("sweetspot", 60, 30),
]


def _band_from_scan(scan, band):
    return {"z1": scan["z1_sec"] + scan["z2_sec"],
            "z2": scan["z3_sec"] + scan["z4_sec"],
            "z3": scan["z5_sec"] + scan["z6_sec"]}[band] / 60.0


class RoundTrip(unittest.TestCase):
    """The load-bearing layer: what it claims is what it writes."""

    @classmethod
    def setUpClass(cls):
        import app
        cls.app = app
        cls.tmp = pathlib.Path(tempfile.mkdtemp())
        cls.built = []
        for st, win, dose in REQUESTS:
            r = wg.generate(st, win, dose)
            if r is None:
                continue
            fn, xml, s = r
            p = cls.tmp / fn
            p.write_text(xml, encoding="utf-8")
            cls.built.append((st, win, dose, p, s))

    def test_the_matrix_actually_produced_something(self):
        """Guards every test below from passing vacuously."""
        self.assertGreaterEqual(len(self.built), len(REQUESTS) - 2)

    def test_the_claimed_dose_is_what_the_scanner_measures(self):
        bad = []
        for st, win, dose, p, s in self.built:
            band = s.protocol.band()
            scan = self.app._scan_zwo_for_library(p)
            self.assertIsNotNone(scan, f"{p.name}: the scanner rejected it")
            got = _band_from_scan(scan, band)
            claimed = s.band_s[band] / 60.0
            if abs(got - claimed) > 0.1:
                bad.append(f"{st} {dose}min/{win}min: claims {claimed:.1f}, "
                           f"scanner says {got:.1f} ({p.name})")
        self.assertEqual(bad, [], "\n".join(bad))

    def test_the_scanner_accounts_for_every_second(self):
        for st, win, dose, p, s in self.built:
            scan = self.app._scan_zwo_for_library(p)
            zsum = sum(scan[f"z{i}_sec"] for i in range(1, 7))
            self.assertAlmostEqual(zsum, scan["total_sec"], delta=1.0, msg=p.name)

    def test_the_declared_duration_matches_the_file(self):
        for st, win, dose, p, s in self.built:
            scan = self.app._scan_zwo_for_library(p)
            self.assertAlmostEqual(scan["total_sec"], s.total_s, delta=1.0, msg=p.name)


class ContractWithTheCaller(unittest.TestCase):
    def test_a_session_never_overruns_its_window(self):
        for st, win, dose in REQUESTS:
            r = wg.generate(st, win, dose)
            if r is None:
                continue
            self.assertLessEqual(r[2].total_s, win * 60, f"{st} {dose}/{win}")

    def test_an_impossible_dose_returns_none_rather_than_a_fabrication(self):
        """25 minutes at VO2max inside a 30-minute window, warm-up included, is
        not a session any cited protocol describes. Saying so is the correct
        answer; the caller shortens the dose or falls back to the library."""
        self.assertIsNone(wg.generate("vo2max", 30, 25))
        self.assertIsNone(wg.generate("threshold", 20, 40))

    def test_an_unknown_session_type_returns_none(self):
        self.assertIsNone(wg.generate("not_a_type", 60, 20))

    def test_it_never_exceeds_the_dose_it_was_asked_for_by_much(self):
        """Undershooting is honest -- the protocol's published bounds may not
        reach the dose. Overshooting is not: it spends budget the rest of the
        week needs."""
        for st, win, dose in REQUESTS:
            r = wg.generate(st, win, dose)
            if r is None:
                continue
            got = r[2].band_s[r[2].protocol.band()] / 60.0
            self.assertLessEqual(got, dose + 2.0, f"{st} {dose}/{win}: got {got:.1f}")

    def test_the_same_request_gives_the_same_file(self):
        """Deterministic naming keeps the content classifier and workout_facts
        caches valid across regenerations."""
        for st, win, dose in REQUESTS[:5]:
            a, b = wg.generate(st, win, dose), wg.generate(st, win, dose)
            self.assertEqual(a is None, b is None)
            if a:
                self.assertEqual(a[0], b[0])
                self.assertEqual(a[1], b[1])


class StaysInsideThePublishedProtocol(unittest.TestCase):
    def test_every_protocol_cites_a_source(self):
        for key, p in wg.PROTOCOLS.items():
            self.assertTrue(p.source.strip(), key)
            self.assertTrue(p.label.strip(), key)

    # What the papers actually specify, written down here rather than read back
    # out of the table under test. The first version of the bounds check
    # compared the solved parameters to PROTOCOLS -- which is a tautology:
    # widening Rønnestad to 9 sets of 40 reps passed cleanly. A citation is
    # only worth something if changing the numbers has to change this too.
    PUBLISHED = {
        # key:            on_pct, rep_s max,  reps max, sets max
        "ronnestad_30_15": (110.0, 30,        13,       4),   # 3x13x30/15
        "seiler_4x8":      (106.0, 600,       5,        1),   # 4x8min @~106%
        "helgerud_4x4":    (112.0, 240,       5,        1),   # 4x4min
        "vo2_5x3":         (118.0, 240,       6,        1),
        "over_under":      (100.0, 300,       6,        2),
        "threshold_2x20":  (98.0,  1500,      3,        1),
        "sweetspot_3x15":  (90.0,  1200,      4,        1),
    }

    def test_the_table_still_matches_the_papers_it_cites(self):
        for key, (on_pct, rep_max, reps_max, sets_max) in self.PUBLISHED.items():
            p = wg.PROTOCOLS[key]
            self.assertEqual(p.on_pct, on_pct, f"{key} intensity")
            self.assertEqual(p.rep_s[1], rep_max, f"{key} longest rep")
            self.assertEqual(p.reps_per_set[1], reps_max, f"{key} most reps")
            self.assertEqual(p.sets[1], sets_max, f"{key} most sets")

    def test_every_protocol_in_the_table_is_pinned_above(self):
        """A new protocol must arrive with its numbers written down here too."""
        self.assertEqual(set(wg.PROTOCOLS), set(self.PUBLISHED))

    def test_solved_parameters_stay_inside_the_published_bounds(self):
        """A generated session that wanders outside the range its own paper
        used is not evidence-based, it is a guess wearing a citation."""
        for st, win, dose in REQUESTS:
            r = wg.generate(st, win, dose)
            if r is None:
                continue
            s = r[2]
            p = s.protocol
            self.assertTrue(p.rep_s[0] <= s.rep_s <= p.rep_s[1], f"{p.key} rep_s")
            self.assertTrue(p.rest_s[0] <= s.rest_s <= p.rest_s[1], f"{p.key} rest_s")
            self.assertTrue(p.reps_per_set[0] <= s.reps_per_set <= p.reps_per_set[1],
                            f"{p.key} reps")
            self.assertTrue(p.sets[0] <= s.sets <= p.sets[1], f"{p.key} sets")

    def test_every_type_maps_only_to_protocols_that_exist(self):
        for st, keys in wg.PROTOCOLS_FOR_TYPE.items():
            for k in keys:
                self.assertIn(k, wg.PROTOCOLS, f"{st} -> {k}")

    def test_a_hard_type_draws_on_a_hard_protocol(self):
        for k in wg.PROTOCOLS_FOR_TYPE["vo2max"]:
            self.assertEqual(wg.PROTOCOLS[k].band(), "z3", k)

    def test_the_emitted_xml_is_well_formed_and_tagged_generated(self):
        for st, win, dose in REQUESTS[:4]:
            r = wg.generate(st, win, dose)
            if r is None:
                continue
            root = ET.fromstring(r[1])
            self.assertEqual(root.tag, "workout_file")
            tags = {t.get("name") for t in root.iter("tag")}
            self.assertIn("generated", tags)
            self.assertIn(r[2].protocol.key, tags)
            self.assertIn(r[2].protocol.source.split()[0],
                          (root.findtext("description") or ""))


if __name__ == "__main__":
    unittest.main()
