"""v2.0.8 — C1: PowerCurve was wrong (short-duration peaks ~half of intervals.icu;
CP/W' shown as "—") because the self-heal backfill only ran when the rider curve
was EMPTY. A couple of stale rides at the 90-day window edge made the curve
non-empty (but low), so the ~24 real in-window rides were never hydrated with
efforts/streams. Fix (app.py api_profile_power_curve): trigger the backfill
whenever in-window rides are missing efforts (n_missing>0), even when the curve
is already non-empty.
"""
import unittest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient


class TestPowerCurveSelfHealGate(unittest.TestCase):
    def _hit(self, rider_curve, n_missing):
        import app as appmod
        import power_curve
        agg = MagicMock(side_effect=[
            {"rider_curve": rider_curve, "n_rides": 24},   # initial (stale/low)
            {"rider_curve": [[5, 1055]], "n_rides": 24},   # after backfill (correct)
        ])
        with patch.object(power_curve, "aggregate_power_curve", agg), \
             patch.object(power_curve, "count_rides_missing_efforts",
                          return_value=(24, n_missing)), \
             patch.object(power_curve, "acquire_backfill_lock",
                          return_value=(True, MagicMock())), \
             patch.object(power_curve, "backfill_icu_history",
                          return_value={"backfilled": 24, "already_cached": 0,
                                        "failed": 0}) as bf, \
             patch.object(power_curve, "release_backfill_lock", return_value=None):
            client = TestClient(appmod.app, raise_server_exceptions=False)
            client.get("/api/profile/power-curve?window_days=90")
            return bf

    def test_backfill_runs_when_curve_nonempty_but_rides_missing_efforts(self):
        # The C1 fix: a non-empty BUT stale/low curve with rides still missing
        # efforts must STILL trigger the hydrating backfill (the old gate didn't).
        bf = self._hit(rider_curve=[[5, 680]], n_missing=24)
        bf.assert_called_once()

    def test_backfill_skipped_when_no_rides_missing_efforts(self):
        bf = self._hit(rider_curve=[[5, 1055]], n_missing=0)
        bf.assert_not_called()


if __name__ == "__main__":
    unittest.main()
