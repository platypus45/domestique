"""v1.3.0 IMPL-POWER-CURVE-CORE — end-to-end tests per PATCH G13.

Locks the chain ``aggregate_power_curve → /api/profile/power-curve`` plus
the backfill task lifecycle visible from the API.

Coverage (4+ tests, per PATCH G13):
  1. Synthetic 30 rides → ``/api/profile/power-curve`` returns the locked
     JSON shape with the expected duration tiers + source attribution.
  2. P&G baseline curve renders with the same duration tiers (one entry
     per ``STANDARD_DURATIONS``).
  3. ``latest_ride_id_in_window`` cache-key invalidates when a new ride
     lands — same window, fresh ride → fresh response, prior cache pruned.
  4. End-to-end: ``POST /api/profile/backfill-history`` → poll status →
     wait completion → ``GET /api/profile/power-curve`` returns the
     expected shape with the rider_curve hydrated by the backfill.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import power_curve
from fitness_estimation import STANDARD_DURATIONS


def _ride(ext_id: str, started_at: str, efforts: list[dict],
          weight_kg: float | None = 70.0, ftp_at_ride: int | None = 250,
          hr_max: int | None = 190) -> dict:
    return {
        "ride_id": f"icu_{ext_id}",
        "external_id": ext_id,
        "source": "icu",
        "name": "synthetic-e2e",
        "started_at": started_at,
        "duration_s": 3600,
        "weight_kg": weight_kg,
        "ftp_at_ride": ftp_at_ride,
        "hr_max": hr_max,
        "efforts": efforts,
    }


def _write_rides(rides: list[dict], target_dir: Path) -> None:
    for r in rides:
        ext = r["external_id"]
        (target_dir / f"{ext}.json").write_text(json.dumps(r), encoding="utf-8")


class PowerCurveEndToEndTests(unittest.TestCase):
    """Tests 1, 2, 3 — chain aggregate_power_curve → endpoint."""

    def setUp(self):
        self._tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"pc_e2e_{os.getpid()}_{id(self)}"
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._patch_dir = patch.object(power_curve, "_icu_rides_dir",
                                        return_value=self._tmp)
        self._patch_dir.start()
        self._patch_prof = patch.object(power_curve, "_profile_ftp_weight",
                                         return_value=(250, 70.0))
        self._patch_prof.start()
        # Bust any preheated cache from prior tests.
        app_module.clear_cache()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch_dir.stop()
        self._patch_prof.stop()
        for f in self._tmp.glob("*"):
            f.unlink(missing_ok=True)
        self._tmp.rmdir()
        app_module.clear_cache()

    def _seed_30_rides(self, peak_ride_id: str = "rPEAK") -> None:
        rides: list[dict] = []
        for i in range(29):
            d = (date.today() - timedelta(days=i + 1)).isoformat()
            rides.append(_ride(
                f"r{i:03d}", d + "T10:00:00",
                efforts=[{"label": "5m", "watts": 280, "secs": 300}],
            ))
        rides.append(_ride(
            peak_ride_id,
            (date.today() - timedelta(days=10)).isoformat() + "T10:00:00",
            efforts=[{"label": "5m", "watts": 320, "secs": 300}],
        ))
        _write_rides(rides, self._tmp)

    def test_endpoint_locked_shape_and_attribution(self):
        """Test 1 — synthetic 30 rides → locked JSON shape with source attribution."""
        self._seed_30_rides()
        r = self.client.get("/api/profile/power-curve?window_days=90")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        # Locked top-level keys.
        for k in ("window_days", "n_rides", "weight_kg", "current_ftp",
                  "rider_curve", "pg_2011_baseline", "cp_w", "wprime_j",
                  "pmax_w"):
            self.assertIn(k, data, f"missing top-level key: {k}")
        self.assertEqual(data["window_days"], 90)
        self.assertEqual(data["n_rides"], 30)
        # Each rider point carries the locked sub-keys.
        for pt in data["rider_curve"]:
            for k in ("duration_s", "watts", "watts_per_kg", "pct_ftp",
                      "ride_id", "date"):
                self.assertIn(k, pt, f"rider point missing key {k}: {pt}")
        # Find the 5-min point and verify watts + attribution.
        five_min = next((p for p in data["rider_curve"]
                         if p["duration_s"] == 300), None)
        self.assertIsNotNone(five_min)
        self.assertEqual(five_min["watts"], 320)
        self.assertEqual(five_min["ride_id"], "icu_rPEAK")

    def test_endpoint_pg_baseline_tier_count(self):
        """Test 2 — P&G baseline has one entry per STANDARD_DURATIONS tier."""
        self._seed_30_rides()
        r = self.client.get("/api/profile/power-curve?window_days=90")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(data["pg_2011_baseline"]), len(STANDARD_DURATIONS))
        # Every locked sub-key present; W = W/kg × current_weight.
        for pt in data["pg_2011_baseline"]:
            for k in ("duration_s", "watts_per_kg", "watts_at_current_weight"):
                self.assertIn(k, pt)
            self.assertEqual(
                pt["watts_at_current_weight"],
                round(pt["watts_per_kg"] * data["weight_kg"]),
            )

    def test_cache_invalidates_on_new_ride(self):
        """Test 3 — cache key includes latest_ride_id_in_window. Drop in a
        new ride → next request returns the updated peaks; the prior
        cache entry is GC'd at the same time per the lazy-prune branch in
        the endpoint."""
        self._seed_30_rides()
        r1 = self.client.get("/api/profile/power-curve?window_days=90")
        self.assertEqual(r1.status_code, 200)
        peak_before = next(p for p in r1.json()["rider_curve"]
                           if p["duration_s"] == 300)
        self.assertEqual(peak_before["watts"], 320)

        # Drop a fresh ride with a HIGHER 5-min that lands inside the window.
        new_ride = _ride(
            "rNEW", (date.today() - timedelta(days=0)).isoformat() + "T18:00:00",
            efforts=[{"label": "5m", "watts": 340, "secs": 300}],
        )
        _write_rides([new_ride], self._tmp)

        r2 = self.client.get("/api/profile/power-curve?window_days=90")
        self.assertEqual(r2.status_code, 200)
        peak_after = next(p for p in r2.json()["rider_curve"]
                          if p["duration_s"] == 300)
        # Cache invalidated → new peak surfaces.
        self.assertEqual(peak_after["watts"], 340)
        self.assertEqual(peak_after["ride_id"], "icu_rNEW")

        # Lazy-GC: only ONE power_curve_<pid>_90_* entry remains. The cache
        # key is per-profile since the profiles/<id>/ archive move — derive
        # the prefix the same way the endpoint does.
        _pid = app_module._active_profile_id_or_default()
        keys = [k for k in app_module._cache.keys()
                if k.startswith(f"power_curve_{_pid}_90_")]
        self.assertEqual(len(keys), 1, f"stale cache entries: {keys}")


class BackfillEndToEndTests(unittest.TestCase):
    """Test 4 — backfill flow from POST → poll → completion → fresh GET."""

    def setUp(self):
        self._tmp_rides = Path(os.environ.get("TMPDIR", "/tmp")) / f"pc_e2e_bf_r_{os.getpid()}_{id(self)}"
        self._tmp_rides.mkdir(parents=True, exist_ok=True)
        self._tmp_lock_dir = Path(os.environ.get("TMPDIR", "/tmp")) / f"pc_e2e_bf_l_{os.getpid()}_{id(self)}"
        self._tmp_lock_dir.mkdir(parents=True, exist_ok=True)
        self._patch_rides = patch.object(power_curve, "_icu_rides_dir",
                                          return_value=self._tmp_rides)
        self._patch_rides.start()
        self._patch_lock = patch.object(power_curve, "_backfill_lock_path",
                                          return_value=self._tmp_lock_dir / ".backfill.lock")
        self._patch_lock.start()
        self._patch_prof = patch.object(power_curve, "_profile_ftp_weight",
                                         return_value=(250, 70.0))
        self._patch_prof.start()
        app_module.clear_cache()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch_rides.stop()
        self._patch_lock.stop()
        self._patch_prof.stop()
        for f in self._tmp_rides.glob("*"):
            f.unlink(missing_ok=True)
        self._tmp_rides.rmdir()
        for f in self._tmp_lock_dir.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        self._tmp_lock_dir.rmdir()
        app_module.clear_cache()

    def test_backfill_then_curve(self):
        """Test 4 — empty efforts → POST backfill → poll → done →
        GET power-curve returns hydrated rider_curve."""
        # Seed a ride that needs backfill (efforts empty).
        ride = _ride("rBF", (date.today() - timedelta(days=5)).isoformat() + "T10:00:00",
                     efforts=[])
        _write_rides([ride], self._tmp_rides)

        # Streams payload long enough to cover all STANDARD_DURATIONS.
        n = 4000
        # Ramped power so different-duration windows pick up different peaks
        # (max-mean over the longest window is lowest — that's expected).
        powers = [200 + (i % 100) for i in range(n)]
        hrs = [140 + (i % 30) for i in range(n)]
        def _streams(_id):
            return {"watts": powers, "heartrate": hrs}

        with patch("training.fetch_activity_streams", side_effect=_streams):
            # Kick off the backfill via the POST endpoint.
            r_post = self.client.post("/api/profile/backfill-history")
            self.assertEqual(r_post.status_code, 200)
            body = r_post.json()
            self.assertEqual(body["status"], "started")
            task_id = body["task_id"]

            # Poll until done (worker runs in a daemon thread; rate-limit at
            # 1 req/sec → only 1 ride means ~1 s. Bound the poll loop at 30
            # iterations × 0.2 s = 6 s.)
            done = False
            for _ in range(30):
                r_status = self.client.get(
                    f"/api/profile/backfill-history/status?task_id={task_id}")
                self.assertEqual(r_status.status_code, 200)
                state = r_status.json().get("state")
                if state == "done":
                    done = True
                    break
                if state == "error":
                    self.fail(f"backfill errored: {r_status.json()}")
                time.sleep(0.2)
            self.assertTrue(done, "backfill never reached state=done")

        # Verify the ride file was rewritten with full STANDARD_DURATIONS.
        path = self._tmp_rides / "rBF.json"
        rewritten = json.loads(path.read_text())
        cached_secs = {e["secs"] for e in rewritten["efforts"]}
        self.assertTrue(cached_secs.issuperset(set(STANDARD_DURATIONS)),
                        f"backfill missing tiers: want⊇{set(STANDARD_DURATIONS)}, got {cached_secs}")

        # And /api/profile/power-curve now shows hydrated rider_curve.
        r_curve = self.client.get("/api/profile/power-curve?window_days=90&refresh=1")
        self.assertEqual(r_curve.status_code, 200)
        data = r_curve.json()
        # At least one rider point per STANDARD_DURATIONS tier.
        durations_in_curve = {p["duration_s"] for p in data["rider_curve"]}
        self.assertTrue(durations_in_curve.issuperset(set(STANDARD_DURATIONS)),
                        f"rider_curve missing tiers: want⊇{set(STANDARD_DURATIONS)}, got {durations_in_curve}")
        # Each point has the locked sub-keys including source attribution.
        for pt in data["rider_curve"]:
            self.assertEqual(pt["ride_id"], "icu_rBF")


if __name__ == "__main__":
    unittest.main()
