"""v2.2.10 — RR-artifact filter must not cascade on a normal HR trend.

The shipped bug compared each beat to the last-ACCEPTED beat, so a smooth warm-up
(RR gliding 0.86 -> 0.55 s) or a single bad early beat made the reference stick
and dropped ~99% of beats -> 0 DFA windows -> a bogus "no_rr_data" / blank panel.
The fix uses the median of the +/-2 surrounding beats (Lipponen-Tarvainen / Kubios).
These lock: (1) no cascade on a trend, (2) no cascade from a bad first beat,
(3) genuine artifacts still dropped, (4) the chokepoint still produces a sane α1.
"""
import unittest

import analytics


class TestArtifactFilterNoCascade(unittest.TestCase):
    def test_smooth_warmup_trend_keeps_almost_everything(self):
        # 600 beats gliding 0.86 -> 0.55 s (HR 70 -> 109): each step tiny, the
        # series is artifact-free. Must keep ~all of it.
        rr = [0.86 - 0.31 * (i / 599) for i in range(600)]
        kept, dropped = analytics._filter_rr_artifacts(rr)
        self.assertLess(dropped, 0.05 * len(rr),
                        f"smooth trend cascaded: dropped {dropped}/{len(rr)}")

    def test_bad_first_beat_does_not_cascade(self):
        # A single dropout/sentinel first beat then a clean ride. Last-accepted
        # cascaded to ~100%; median must drop ~1.
        rr = [1.40] + [0.80] * 400
        kept, dropped = analytics._filter_rr_artifacts(rr)
        self.assertLessEqual(dropped, 3, f"bad first beat cascaded: dropped {dropped}")
        self.assertGreater(len(kept), 390)

    def test_isolated_ectopic_is_dropped(self):
        # Clean 0.90 s series with one ectopic doublet (short + compensatory long).
        # The artifacts must be rejected (filter still works).
        rr = [0.90] * 20
        rr[10] = 0.55  # ectopic
        rr[11] = 1.25  # compensatory pause
        kept, dropped = analytics._filter_rr_artifacts(rr)
        self.assertGreaterEqual(dropped, 2, "ectopic doublet not rejected")
        self.assertNotIn(0.55, kept)
        self.assertNotIn(1.25, kept)
        # The clean beats around the ectopic must survive.
        self.assertGreaterEqual(len(kept), 16)


if __name__ == "__main__":
    unittest.main()
