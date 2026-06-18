"""v2.2 — N5 (G1): outdoor-variant ZWO wrapper.

Frames a prescribed indoor block inside a real outdoor ride: a flat transit
warm-up to the climb + an easy spin home, with the prescribed body passed through
UNCHANGED. It's the download path only, so the transit/spin minutes are OFF-PLAN
(never folded into the planner's accounted weekly TSS) by construction.
"""
import unittest

import app

SAMPLE = (
    '<workout_file>\n'
    '    <name>Test session</name>\n'
    '    <workout>\n'
    '        <Warmup Duration="600" PowerLow="0.50" PowerHigh="0.70"/>\n'
    '        <IntervalsT Repeat="4" OnDuration="240" OffDuration="120" OnPower="1.10" OffPower="0.50"/>\n'
    '        <Cooldown Duration="300" PowerLow="0.50" PowerHigh="0.40"/>\n'
    '    </workout>\n'
    '</workout_file>\n'
)
# prescribed-body lines that must survive verbatim
_BODY_LINES = [
    '<Warmup Duration="600" PowerLow="0.50" PowerHigh="0.70"/>',
    '<IntervalsT Repeat="4" OnDuration="240" OffDuration="120" OnPower="1.10" OffPower="0.50"/>',
    '<Cooldown Duration="300" PowerLow="0.50" PowerHigh="0.40"/>',
]


class TestOutdoorWrapper(unittest.TestCase):
    def test_wraps_with_transit_and_spin_home(self):
        out = app._wrap_zwo_outdoor(SAMPLE, transit_min=12, spin_min=25)
        # transit warm-up (12*60=720) prepended; spin-home cooldown (25*60=1500) appended
        self.assertIn('<Warmup Duration="720"', out)
        self.assertIn('<Cooldown Duration="1500"', out)
        # prescribed body passed through unchanged
        for line in _BODY_LINES:
            self.assertIn(line, out)
        # ordering: transit before the body's own warmup; spin-home after its cooldown
        self.assertLess(out.index('Duration="720"'), out.index('Duration="600"'))
        self.assertGreater(out.index('Duration="1500"'), out.index('Duration="300"'))

    def test_disabled_is_identical(self):
        # transit=0 and spin=0 → no wrapping, byte-identical to the source
        self.assertEqual(app._wrap_zwo_outdoor(SAMPLE, 0, 0), SAMPLE)

    def test_single_workout_block_only(self):
        out = app._wrap_zwo_outdoor(SAMPLE, 10, 10)
        # exactly one transit + one spin-home added (not duplicated), one workout block
        self.assertEqual(out.count('G1 transit'), 1)
        self.assertEqual(out.count('G1 spin home'), 1)
        self.assertEqual(out.count('</workout>'), 1)


if __name__ == "__main__":
    unittest.main()
