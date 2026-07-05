"""v2.1.0 — C1: PowerCurve was wrong (short-duration peaks ~half of intervals.icu;
CP/W' shown as "—") because the self-heal backfill only ran when the rider curve
was EMPTY. A couple of stale rides at the 90-day window edge made the curve
non-empty (but low), so the ~24 real in-window rides were never hydrated with
efforts/streams. Fix (app.py api_profile_power_curve): trigger the backfill
whenever in-window rides are missing efforts (n_missing>0), even when the curve
is already non-empty.
"""
import time
import unittest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient


class TestPowerCurveSelfHealGate(unittest.TestCase):
    def _hit(self, rider_curve, n_missing):
        import app as appmod
        import power_curve
        appmod._pc_backfill_running.clear()  # v3.2.1: reset single-flight guard
        appmod._cache.clear(); appmod._cache_ts.clear()  # avoid 24h cross-test cache
        agg = MagicMock(return_value={"rider_curve": rider_curve, "n_rides": 24})
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
            resp = client.get("/api/profile/power-curve?window_days=90").json()
            # v3.2.1: the backfill now runs in a BACKGROUND daemon thread —
            # poll briefly for it to fire (mocked, so near-instant).
            for _ in range(50):
                if bf.called:
                    break
                time.sleep(0.02)
            return bf, resp

    def test_backfill_runs_when_curve_nonempty_but_rides_missing_efforts(self):
        # C1: rides missing efforts must STILL trigger the hydrating backfill.
        # v3.2.1: it's kicked in the BACKGROUND (endpoint no longer blocks) and
        # the response carries backfill_progress so the frontend can poll.
        bf, resp = self._hit(rider_curve=[[5, 680]], n_missing=24)
        bf.assert_called_once()
        self.assertIn("backfill_progress", resp)

    def test_backfill_skipped_when_no_rides_missing_efforts(self):
        bf, resp = self._hit(rider_curve=[[5, 1055]], n_missing=0)
        bf.assert_not_called()
        self.assertNotIn("backfill_progress", resp)


if __name__ == "__main__":
    unittest.main()
