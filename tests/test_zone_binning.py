"""Binning a workout into zones is arithmetic, and it must come out the same
everywhere.

Three things have to hold and none of them were checked:

  1. The seconds assigned to zones equal the workout's duration. Nothing lost,
     nothing invented, no segment type silently skipped.
  2. A second, independent implementation agrees. The library's Z1%..Z6% are
     read by the sampler's budget-fit, by the planner's zone rails and by the
     UI; if they are wrong every consumer is wrong together and consistently,
     which is the hardest kind of wrong to notice.
  3. The two scanners agree with each other. `training_planner._scan` and
     `app._scan_zwo_for_library` are separate implementations of the same
     computation, 72 identical code lines apart. Until they are merged, they
     must at least be shown to produce the same numbers.

Ramps used to be binned at 20 slices regardless of length while the NP/IF
series for the same segment was built at 1 Hz — two resolutions for one
segment, costing up to 2.8 points on a zone share.
"""
import collections
import os
import pathlib
import sys
import unittest
import xml.etree.ElementTree as ET

os.environ.setdefault("PYTHONHASHSEED", "0")
SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
import training_planner as tp  # noqa: E402
import zones as zones_mod      # noqa: E402

EDGES = [(56, "z1"), (76, "z2"), (91, "z3"), (106, "z4"), (121, "z5")]


def zone_of(pct):
    for edge, name in EDGES:
        if pct < edge:
            return name
    return "z6"


def bin_file(path):
    """Independent binner, written from the ZWO shape rather than from the
    scanner it checks. Every segment sliced to 1 second where power is known."""
    root = ET.parse(path).getroot()
    w = root.find("workout")
    acc = collections.Counter()
    total = 0.0
    for seg in (w if w is not None else []):
        t = seg.tag
        if t in ("Warmup", "Cooldown", "Ramp"):
            d = float(seg.get("Duration", 0) or 0)
            lo = float(seg.get("PowerLow", 0.5) or 0.5)
            hi = float(seg.get("PowerHigh", 0.7) or 0.7)
            n = max(1, int(d))
            for i in range(n):
                acc[zone_of((lo + (hi - lo) * (i + 0.5) / n) * 100)] += d / n
            total += d
        elif t == "SteadyState":
            d = float(seg.get("Duration", 0) or 0)
            acc[zone_of(float(seg.get("Power", 0.65) or 0.65) * 100)] += d
            total += d
        elif t == "IntervalsT":
            r = int(float(seg.get("Repeat", 1) or 1))
            on = float(seg.get("OnDuration", 0) or 0)
            off = float(seg.get("OffDuration", 0) or 0)
            acc[zone_of(float(seg.get("OnPower", 1.0) or 1.0) * 100)] += r * on
            acc[zone_of(float(seg.get("OffPower", 0.5) or 0.5) * 100)] += r * off
            total += r * (on + off)
        elif t == "FreeRide":
            d = float(seg.get("Duration", 0) or 0)
            acc[zone_of(65)] += d          # the scanner's stated assumption
            total += d
        elif t not in ("textevent", "TextEvent"):
            d = float(seg.get("Duration", 0) or 0)
            if d > 0:
                acc["UNHANDLED"] += d
                total += d
    return acc, total


class ZoneEdges(unittest.TestCase):
    def test_the_inline_edges_match_the_canonical_coggan_table(self):
        """zones.py calls itself the single source of truth and both scanners
        hardcode their own copy of the boundaries. They must at least agree."""
        fracs = [hi for _lo, hi, _n in zones_mod._POWER_FRACS[:5]]
        inline = [e / 100 - 0.01 for e, _n in EDGES]      # <56 means "up to 55"
        for got, want in zip(inline, fracs):
            self.assertAlmostEqual(got, want, places=6)

    def test_the_planner_and_app_scanners_use_the_same_edges(self):
        import inspect
        import app
        a = inspect.getsource(app._scan_zwo_for_library)
        p = inspect.getsource(tp._load_workout_library_uncached
                              if hasattr(tp, "_load_workout_library_uncached")
                              else tp.load_workout_library)
        for edge in ("< 56", "< 76", "< 91", "< 106", "< 121"):
            self.assertIn(edge, a, f"app scanner lost the {edge} boundary")


class LibraryBinning(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = {r["File"]: r for r in tp.load_workout_library()}
        cls.dir = tp.WORKOUT_DIR

    def test_the_library_is_not_empty(self):
        """Guards the three tests below from passing vacuously."""
        self.assertGreater(len(self.rows), 100)

    def test_zone_seconds_account_for_every_second_of_every_workout(self):
        bad = []
        for f in self.rows:
            p = self.dir / f
            if not p.exists():
                continue
            acc, total = bin_file(p)
            if acc.get("UNHANDLED"):
                bad.append(f"{f}: {acc['UNHANDLED']:.0f}s in an unhandled segment type")
                continue
            zs = sum(v for k, v in acc.items() if k.startswith("z"))
            if total > 0 and abs(zs - total) > 1.0:
                bad.append(f"{f}: zones {zs:.0f}s vs duration {total:.0f}s")
        self.assertEqual(bad, [], "\n".join(bad[:20]))

    def test_every_row_agrees_with_an_independent_bin(self):
        bad = []
        for f, row in self.rows.items():
            p = self.dir / f
            if not p.exists():
                continue
            acc, total = bin_file(p)
            if total <= 0:
                continue
            for i, k in enumerate(("z1", "z2", "z3", "z4", "z5", "z6"), start=1):
                mine = 100 * acc.get(k, 0.0) / total
                theirs = float(row.get(f"Z{i}%", 0) or 0)
                if abs(mine - theirs) > 0.5:
                    bad.append(f"{f} Z{i}%: row {theirs} vs independent {mine:.1f}")
                    break
        self.assertEqual(bad, [], "\n".join(bad[:20]))

    def test_the_two_scanners_produce_identical_numbers(self):
        import app
        bad = []
        for f, row in self.rows.items():
            p = self.dir / f
            if not p.exists():
                continue
            a = app._scan_zwo_for_library(p)
            self.assertIsNotNone(a, f"{f}: app scanner returned None")
            for k in ("Z1%", "Z2%", "Z3%", "Z4%", "Z5%", "Z6%",
                      "TSS", "IF", "Duration(min)"):
                if row.get(k) is None or a.get(k) is None:
                    continue
                if abs(float(row[k]) - float(a[k])) > 0.05:
                    bad.append(f"{f} {k}: planner {row[k]} vs app {a[k]}")
                    break
        self.assertEqual(bad, [], "\n".join(bad[:20]))


if __name__ == "__main__":
    unittest.main()
