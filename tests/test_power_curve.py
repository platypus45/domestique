"""v1.3.0 IMPL-POWER-CURVE-CORE — unit tests for power_curve.py.

Locks the contracts spelled out in:
  /tmp/MASTER_DECISIONS_v130.md           (original plan)
  /tmp/MASTER_DECISIONS_v130_PATCH.md     (overrides on conflict)
  /tmp/audit_v130_power_curve.md          (audit acceptance gates §3)

Coverage (12 tests):
  1.  aggregate_power_curve synthetic 30-ride fixture → expected peaks
      with correct source-ride attribution.
  2.  Window filtering: 30 / 90 / 180 / 365 / all distinct results on a
      synthetic 365-day spread.
  3.  is_sensor_glitch returns True for 1-s peak with HR=80 + HR_max=180.
  4.  is_sensor_glitch returns False for a 30-s peak (NOT 1-s) — per
      PATCH G9 the filter is 1-s only, even when HR is low.
  5.  is_sensor_glitch returns False for a 1-s peak with HR=120 + HR_max=180
      (HR > 50 % HR_max → genuine).
  6.  compute_ride_prs major vs minor tier classification (≥5 W or ≥2 % vs 1-5 W).
  7.  compute_ride_prs returns the FULL list (G7 — no UI cap).
  8.  backfill_icu_history idempotent: skip a ride that already has full
      STANDARD_DURATIONS coverage.
  9.  backfill_icu_history atomic-write: a simulated mid-write crash
      leaves NO partial file.
  10. backfill_icu_history single-flight: a second concurrent call returns
      ``already_running``.
  11. P&G 2011 baseline overlay rendered with correct W/kg → W and W/kg →
      %FTP scaling using the current profile weight + FTP.
  12. ``_aggregate_best_efforts_90d`` shim returns the same 4-tier dict
      shape as the original (regression guard).
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import power_curve
from fitness_estimation import STANDARD_DURATIONS

REPO_ROOT = Path(__file__).resolve().parent.parent


# Helper to build a synthetic ride dict in the v1.0.6 ICU envelope shape.
def _ride(ext_id: str, started_at: str, efforts: list[dict],
          weight_kg: float | None = 70.0, ftp_at_ride: int | None = 250,
          hr_max: int | None = 190) -> dict:
    return {
        "ride_id": f"icu_{ext_id}",
        "external_id": ext_id,
        "source": "icu",
        "name": "synthetic",
        "started_at": started_at,
        "duration_s": 3600,
        "weight_kg": weight_kg,
        "ftp_at_ride": ftp_at_ride,
        "hr_max": hr_max,
        "efforts": efforts,
    }


def _write_rides_to_dir(rides: list[dict], target_dir: Path) -> None:
    for r in rides:
        ext = r["external_id"]
        (target_dir / f"{ext}.json").write_text(json.dumps(r), encoding="utf-8")


class PowerCurveAggregationTests(unittest.TestCase):
    """Tests 1, 2, 11 — aggregate_power_curve correctness + windows + P&G overlay."""

    def setUp(self):
        self._tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"pc_agg_{os.getpid()}_{id(self)}"
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._patch_dir = patch.object(power_curve, "_icu_rides_dir",
                                        return_value=self._tmp)
        self._patch_dir.start()
        # Patch profile to deterministic FTP/weight.
        self._patch_prof = patch.object(power_curve, "_profile_ftp_weight",
                                         return_value=(250, 70.0))
        self._patch_prof.start()

    def tearDown(self):
        self._patch_dir.stop()
        self._patch_prof.stop()
        for f in self._tmp.glob("*"):
            f.unlink(missing_ok=True)
        self._tmp.rmdir()

    def test_synthetic_30_ride_fixture_peaks(self):
        """Test 1 — 30 rides with known 5-min PR of 320 W → curve picks it up
        AND attributes it to the right ride_id."""
        from datetime import date, timedelta
        rides: list[dict] = []
        # 29 rides with 280 W 5-min, 1 ride with 320 W 5-min.
        for i in range(29):
            d = (date.today() - timedelta(days=i + 1)).isoformat()
            rides.append(_ride(
                f"r{i:03d}", d + "T10:00:00",
                efforts=[{"label": "5m", "watts": 280, "secs": 300}],
            ))
        # The peak ride is 10 days ago.
        rides.append(_ride(
            "rPEAK", (date.today() - timedelta(days=10)).isoformat() + "T10:00:00",
            efforts=[{"label": "5m", "watts": 320, "secs": 300}],
        ))
        _write_rides_to_dir(rides, self._tmp)

        out = power_curve.aggregate_power_curve("default", window_days=90)
        self.assertEqual(out["n_rides"], 30)
        # find the 5-min point
        five_min = next((p for p in out["rider_curve"]
                         if p["duration_s"] == 300), None)
        self.assertIsNotNone(five_min)
        self.assertEqual(five_min["watts"], 320)
        self.assertEqual(five_min["ride_id"], "icu_rPEAK")

    def test_window_filtering_distinct(self):
        """Test 2 — windows 30/90/180/365 give distinct results when each
        bucket has a different best 5-min effort.

        Anchor each bucket comfortably away from the edges so date-arithmetic
        rounding doesn't push a ride into the wrong window:
            ride_30  at day-15  (counts in 30/90/180/365)
            ride_90  at day-60  (counts in 90/180/365 only)
            ride_180 at day-150 (counts in 180/365 only)
            ride_365 at day-300 (counts in 365 only)
        """
        from datetime import date, timedelta
        rides = [
            _ride("r30",  (date.today() - timedelta(days=15)).isoformat()  + "T10:00:00",
                  efforts=[{"label": "5m", "watts": 295, "secs": 300}]),
            _ride("r90",  (date.today() - timedelta(days=60)).isoformat()  + "T10:00:00",
                  efforts=[{"label": "5m", "watts": 305, "secs": 300}]),
            _ride("r180", (date.today() - timedelta(days=150)).isoformat() + "T10:00:00",
                  efforts=[{"label": "5m", "watts": 310, "secs": 300}]),
            _ride("r365", (date.today() - timedelta(days=300)).isoformat() + "T10:00:00",
                  efforts=[{"label": "5m", "watts": 320, "secs": 300}]),
        ]
        _write_rides_to_dir(rides, self._tmp)

        peaks = {}
        for w in (30, 90, 180, 365):
            out = power_curve.aggregate_power_curve("default", window_days=w)
            five = next((p for p in out["rider_curve"]
                         if p["duration_s"] == 300), None)
            self.assertIsNotNone(five, f"no 5m peak in window={w}")
            peaks[w] = five["watts"]

        self.assertEqual(peaks[30], 295)
        self.assertEqual(peaks[90], 305)
        self.assertEqual(peaks[180], 310)
        self.assertEqual(peaks[365], 320)
        # All four distinct.
        self.assertEqual(len(set(peaks.values())), 4)

    def test_v189_bug1_pct_ftp_always_positive_when_watts_set(self):
        """v1.8.9 Bug 1 — pct_ftp must be a positive number for every point
        whose watts > 0, even when per-ride ftp_at_ride is missing or zero
        (falls back to current profile FTP). Locks master §1 contract."""
        from datetime import date, timedelta
        # Three rides: one with ftp_at_ride=250, one with ftp_at_ride=0,
        # one with ftp_at_ride=None. All have watts > 0 — every emitted
        # point must have pct_ftp > 0.
        rides = [
            _ride("rA",
                  (date.today() - timedelta(days=5)).isoformat() + "T10:00:00",
                  efforts=[{"label": "5m", "watts": 290, "secs": 300}],
                  ftp_at_ride=250),
            _ride("rB",
                  (date.today() - timedelta(days=4)).isoformat() + "T10:00:00",
                  efforts=[{"label": "1m", "watts": 380, "secs": 60}],
                  ftp_at_ride=0),
            _ride("rC",
                  (date.today() - timedelta(days=3)).isoformat() + "T10:00:00",
                  efforts=[{"label": "20m", "watts": 270, "secs": 1200}],
                  ftp_at_ride=None),
        ]
        _write_rides_to_dir(rides, self._tmp)
        out = power_curve.aggregate_power_curve("default", window_days=90)
        self.assertEqual(out["n_rides"], 3)
        rc = out["rider_curve"]
        self.assertTrue(rc, "rider_curve must be populated")
        for pt in rc:
            if pt.get("watts") and pt["watts"] > 0:
                self.assertIsNotNone(pt.get("pct_ftp"),
                    f"pct_ftp None for {pt['duration_s']}s watts={pt['watts']}")
                self.assertGreater(pt["pct_ftp"], 0,
                    f"pct_ftp not positive for {pt['duration_s']}s")

    def test_v189_bug2_30d_window_returns_curve(self):
        """v1.8.9 Bug 2 — /api/profile/power-curve?window_days=30 must return
        a populated rider_curve when ANY rides exist in last 30d. No min-
        window clamp. Master §2 contract."""
        from datetime import date, timedelta
        # Two rides — one inside 30d, one outside. window_days=30 must
        # include the inside-30d ride.
        rides = [
            _ride("rIN",
                  (date.today() - timedelta(days=10)).isoformat() + "T10:00:00",
                  efforts=[{"label": "5m", "watts": 280, "secs": 300}]),
            _ride("rOUT",
                  (date.today() - timedelta(days=60)).isoformat() + "T10:00:00",
                  efforts=[{"label": "5m", "watts": 320, "secs": 300}]),
        ]
        _write_rides_to_dir(rides, self._tmp)
        out = power_curve.aggregate_power_curve("default", window_days=30)
        self.assertEqual(out["n_rides"], 1)
        self.assertTrue(out["rider_curve"],
            "30d window must return populated rider_curve when rides exist")
        five = next((p for p in out["rider_curve"]
                     if p["duration_s"] == 300), None)
        self.assertIsNotNone(five)
        self.assertEqual(five["watts"], 280)

    def test_pg_baseline_scaling(self):
        """Test 11 — P&G baseline returns W/kg + watts_at_current_weight at
        each STANDARD_DURATIONS tier. W = W/kg × current_weight."""
        out = power_curve.aggregate_power_curve("default", window_days=90)
        self.assertEqual(len(out["pg_2011_baseline"]), len(STANDARD_DURATIONS))
        # Every entry has the locked keys.
        for pt in out["pg_2011_baseline"]:
            self.assertIn("duration_s", pt)
            self.assertIn("watts_per_kg", pt)
            self.assertIn("watts_at_current_weight", pt)
            # Sanity: watts ≈ W/kg × profile weight (70.0).
            expected_w = round(pt["watts_per_kg"] * 70.0)
            self.assertEqual(pt["watts_at_current_weight"], expected_w,
                f"P&G baseline {pt['duration_s']}s: "
                f"{pt['watts_per_kg']} × 70.0 != {pt['watts_at_current_weight']}")
        # 5-min anchor: 5.27 W/kg × 70 ≈ 369 W
        five_min = next(p for p in out["pg_2011_baseline"]
                        if p["duration_s"] == 300)
        self.assertAlmostEqual(five_min["watts_per_kg"], 5.27, places=2)
        self.assertAlmostEqual(five_min["watts_at_current_weight"], 369, delta=1)


class SensorGlitchFilterTests(unittest.TestCase):
    """Tests 3, 4, 5 — is_sensor_glitch (G16 + G9): 1-s sub-HR ONLY."""

    def test_one_second_low_hr_is_glitch(self):
        """Test 3 — 1-s peak with HR=80 + HR_max=180 → True (a wireless dropout)."""
        eff = {"label": "1s", "watts": 1500, "secs": 1, "hr": 80}
        ride = {"hr_max": 180}
        profile = {}
        self.assertTrue(power_curve.is_sensor_glitch(eff, ride, profile))

    def test_thirty_second_low_hr_is_genuine(self):
        """Test 4 — 30-s peak with HR=80 → False (HR-lag is real on a sprint).
        Per PATCH G9 the filter is 1-s only."""
        eff = {"label": "30s", "watts": 700, "secs": 30, "hr": 80}
        ride = {"hr_max": 180}
        profile = {}
        self.assertFalse(power_curve.is_sensor_glitch(eff, ride, profile))

    def test_one_second_normal_hr_is_genuine(self):
        """Test 5 — 1-s peak with HR=120 + HR_max=180 → False.
        120/180 = 67 % > 50 % cutoff."""
        eff = {"label": "1s", "watts": 1200, "secs": 1, "hr": 120}
        ride = {"hr_max": 180}
        profile = {}
        self.assertFalse(power_curve.is_sensor_glitch(eff, ride, profile))


class PerRidePRsTests(unittest.TestCase):
    """Tests 6, 7 — compute_ride_prs major/minor tiers + full list returned."""

    def setUp(self):
        self._tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"pc_pr_{os.getpid()}_{id(self)}"
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._patch_dir = patch.object(power_curve, "_icu_rides_dir",
                                        return_value=self._tmp)
        self._patch_dir.start()

    def tearDown(self):
        self._patch_dir.stop()
        for f in self._tmp.glob("*"):
            f.unlink(missing_ok=True)
        self._tmp.rmdir()

    def test_major_vs_minor_tier(self):
        """Test 6 — verify the tier rules:
            ≥5 W or ≥2 %  → major
            1-5 W and <2 % → minor"""
        from datetime import date, timedelta
        # Prior ride: 200 W @ 5-min, 100 W @ 1-min, 500 W @ 5-s.
        # Today's ride bumps each by a different amount:
        #   5-min: 200 → 220   (Δ=20 W = +10 %  → MAJOR — both gates pass)
        #   1-min: 100 → 102   (Δ=2 W = +2.0 %  → MAJOR — pct gate, not W gate)
        #   5-s:   500 → 503   (Δ=3 W = +0.6 %  → MINOR — both fail major)
        prior = _ride(
            "rPRIOR",
            (date.today() - timedelta(days=10)).isoformat() + "T10:00:00",
            efforts=[
                {"label": "5m", "watts": 200, "secs": 300},
                {"label": "1m", "watts": 100, "secs": 60},
                {"label": "5s", "watts": 500, "secs": 5},
            ],
        )
        today = _ride(
            "rTODAY",
            (date.today() - timedelta(days=1)).isoformat() + "T10:00:00",
            efforts=[
                {"label": "5m", "watts": 220, "secs": 300},
                {"label": "1m", "watts": 102, "secs": 60},
                {"label": "5s", "watts": 503, "secs": 5},
            ],
        )
        _write_rides_to_dir([prior, today], self._tmp)

        prs = power_curve.compute_ride_prs("icu_rTODAY", window_days=90)
        by_dur = {p["duration_s"]: p for p in prs}
        self.assertIn(300, by_dur)
        self.assertEqual(by_dur[300]["tier"], "major")
        self.assertEqual(by_dur[300]["exceedance_w"], 20)

        self.assertIn(60, by_dur)
        self.assertEqual(by_dur[60]["tier"], "major")  # 2.0 % ≥ 2 % gate
        self.assertEqual(by_dur[60]["exceedance_w"], 2)

        self.assertIn(5, by_dur)
        self.assertEqual(by_dur[5]["tier"], "minor")
        self.assertEqual(by_dur[5]["exceedance_w"], 3)

    def test_returns_full_list_no_cap(self):
        """Test 7 — G7: function returns the FULL list with no UI cap."""
        from datetime import date, timedelta
        # Prior baseline at every tier; today exceeds every tier by ≥1 W.
        prior_efforts = [{"label": f"{d}s", "watts": 100, "secs": d}
                         for d in (1, 5, 30, 60, 300, 1200)]
        today_efforts = [{"label": f"{d}s", "watts": 102, "secs": d}
                         for d in (1, 5, 30, 60, 300, 1200)]
        prior = _ride(
            "rPRIOR",
            (date.today() - timedelta(days=10)).isoformat() + "T10:00:00",
            efforts=prior_efforts,
        )
        today = _ride(
            "rTODAY",
            (date.today() - timedelta(days=1)).isoformat() + "T10:00:00",
            efforts=today_efforts,
        )
        _write_rides_to_dir([prior, today], self._tmp)

        prs = power_curve.compute_ride_prs("icu_rTODAY", window_days=90)
        self.assertEqual(len(prs), 6)  # full list — not capped


class BackfillTests(unittest.TestCase):
    """Tests 8, 9, 10 — idempotency + atomic writes + single-flight lock."""

    def setUp(self):
        self._tmp_rides = Path(os.environ.get("TMPDIR", "/tmp")) / f"pc_bf_r_{os.getpid()}_{id(self)}"
        self._tmp_rides.mkdir(parents=True, exist_ok=True)
        self._tmp_lock_dir = Path(os.environ.get("TMPDIR", "/tmp")) / f"pc_bf_l_{os.getpid()}_{id(self)}"
        self._tmp_lock_dir.mkdir(parents=True, exist_ok=True)
        self._patch_rides = patch.object(power_curve, "_icu_rides_dir",
                                          return_value=self._tmp_rides)
        self._patch_rides.start()
        self._patch_lock = patch.object(power_curve, "_backfill_lock_path",
                                          return_value=self._tmp_lock_dir / ".backfill.lock")
        self._patch_lock.start()

    def tearDown(self):
        self._patch_rides.stop()
        self._patch_lock.stop()
        for f in self._tmp_rides.glob("*"):
            f.unlink(missing_ok=True)
        self._tmp_rides.rmdir()
        for f in self._tmp_lock_dir.glob(".*"):
            f.unlink(missing_ok=True)
        for f in self._tmp_lock_dir.glob("*"):
            f.unlink(missing_ok=True)
        self._tmp_lock_dir.rmdir()

    def test_idempotent_skips_full_coverage(self):
        """Test 8 — a ride already covering the full STANDARD_DURATIONS set
        AND with streams.watts cached is skipped; backfilled=0,
        already_cached=1.

        v1.8.10 Bug A — ``_needs_refetch`` now ALSO requires
        ``streams.watts`` to be present, so a ride with only ``efforts``
        triggers a refetch (intentional — historic backfills wrote
        efforts but skipped streams, leaving the fatigue panel stuck on
        0%). Seed streams.watts here to keep the idempotency contract.
        """
        # Build a ride with one effort per STANDARD_DURATIONS tier.
        full_efforts = [{"label": f"{d}s", "watts": 100 + d, "secs": d}
                        for d in STANDARD_DURATIONS]
        ride = _ride("rFULL", "2026-04-01T10:00:00", efforts=full_efforts)
        # v1.8.10: streams.watts must be present too, else _needs_refetch
        # returns True (we tightened the gate to fix the 0% stuck bug).
        ride["streams"] = {"watts": [200] * 60, "heartrate": [140] * 60}
        _write_rides_to_dir([ride], self._tmp_rides)

        # No streams fetcher should ever be called — patch fetch_activity_streams
        # to raise to make the test loud if backfill misclassifies coverage.
        called = []
        def _fail_streams(_id):
            called.append(_id)
            raise AssertionError(f"streams fetched for {_id} despite full coverage")
        with patch("training.fetch_activity_streams", side_effect=_fail_streams):
            result = power_curve.backfill_icu_history("default", max_per_second=100)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["backfilled"], 0)
        self.assertEqual(result["already_cached"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertFalse(called, "streams fetched despite full coverage")

    def test_atomic_write_no_partial_file(self):
        """Test 9 — when the rename raises mid-write, no partial file remains.

        Seeds a ride that needs refetching, mocks the streams fetcher to
        return data that drives _extract_efforts_from_streams to non-empty,
        and patches ``os.replace`` to raise ONLY when the destination is
        the ride file (the lock file's replace must still succeed). This
        forces the real ``_atomic_write_json`` to take its except branch,
        which is responsible for cleaning up the tempfile.

        After the simulated crash:
          - the original ride JSON is still intact (no torn write)
          - no tempfile fragment remains in the rides dir
          - backfill reports the failure in its counters
        """
        ride = _ride("rATOMIC", "2026-04-01T10:00:00", efforts=[])
        path = self._tmp_rides / "rATOMIC.json"
        path.write_text(json.dumps(ride), encoding="utf-8")
        original_text = path.read_text()

        # Streams fetcher returns enough power for at least one tier (300 s).
        def _streams(_id):
            n = 700
            return {"watts": [200] * n, "heartrate": [140] * n}

        # Patch os.replace to raise ONLY for the ride-file destination. The
        # tempfile path looks like ".../rATOMIC.json.<rand>" and the real
        # destination ends in /rATOMIC.json — both contain the rides-dir
        # path, so we filter on rides-dir membership of the destination.
        real_replace = os.replace
        rides_dir_str = str(self._tmp_rides)
        def _broken_replace(src, dst):
            if rides_dir_str in str(dst):
                raise OSError("simulated crash mid-rename")
            return real_replace(src, dst)

        with patch("training.fetch_activity_streams", side_effect=_streams), \
             patch("os.replace", side_effect=_broken_replace):
            result = power_curve.backfill_icu_history("default", max_per_second=100)

        # The file must still be the original, untouched.
        self.assertEqual(path.read_text(), original_text,
                         "ride file was clobbered by torn write")
        # No leftover tempfile fragments in the rides dir.
        leftovers = [f for f in self._tmp_rides.iterdir()
                     if f.name != "rATOMIC.json" and f.is_file()]
        self.assertEqual(leftovers, [],
                         f"tempfile leftover after broken rename: {leftovers}")
        # And the result reports the failure.
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["failed"], 1)

    def test_single_flight_lock(self):
        """Test 10 — a second concurrent acquire returns ``already_running``."""
        # First acquire — succeeds.
        ok1, info1 = power_curve.acquire_backfill_lock()
        self.assertTrue(ok1)
        self.assertIn("task_id", info1)

        # Second acquire — must return False with the existing lock data.
        ok2, info2 = power_curve.acquire_backfill_lock()
        self.assertFalse(ok2)
        self.assertEqual(info2.get("task_id"), info1["task_id"])

        # Cleanup.
        power_curve.release_backfill_lock()

    def test_v1810_streams_persisted(self):
        """v1.8.10 Bug A — backfill MUST write ``streams`` to disk, not
        just ``efforts``. Without this the fatigue panel reports 0%
        forever because ``_ride_power_stream`` reads
        ``ride["streams"]["watts"]`` which was never written.
        """
        ride = _ride("rSTREAMS", "2026-04-01T10:00:00", efforts=[])
        _write_rides_to_dir([ride], self._tmp_rides)

        def _streams(_id):
            n = 700
            return {"watts": [200] * n, "heartrate": [140] * n}

        with patch("training.fetch_activity_streams", side_effect=_streams):
            result = power_curve.backfill_icu_history("default", max_per_second=100)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["backfilled"], 1)
        # Read the persisted file: streams must be there, with watts.
        persisted = json.loads(
            (self._tmp_rides / "rSTREAMS.json").read_text(encoding="utf-8")
        )
        self.assertIn("streams", persisted, "streams key absent — bug A regression")
        self.assertIn("watts", persisted["streams"])
        self.assertEqual(len(persisted["streams"]["watts"]), 700)
        # And efforts should also be present (existing contract).
        self.assertIn("efforts", persisted)
        self.assertGreater(len(persisted["efforts"]), 0)

    def test_v1810_needs_refetch_when_only_efforts(self):
        """v1.8.10 Bug A — a ride with full ``efforts`` coverage but NO
        ``streams.watts`` MUST be re-fetched (the historical state that
        caused the 0% stuck bug).
        """
        full_efforts = [{"label": f"{d}s", "watts": 100 + d, "secs": d}
                        for d in STANDARD_DURATIONS]
        ride = _ride("rSTALE", "2026-04-01T10:00:00", efforts=full_efforts)
        # NO streams key — represents pre-v1.8.10 cached state.
        _write_rides_to_dir([ride], self._tmp_rides)

        path = self._tmp_rides / "rSTALE.json"
        self.assertTrue(power_curve._needs_refetch(path),
                        "ride with efforts but no streams must refetch")

        # After we add streams.watts, _needs_refetch flips to False.
        data = json.loads(path.read_text(encoding="utf-8"))
        data["streams"] = {"watts": [200] * 60}
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(power_curve._needs_refetch(path),
                         "ride with efforts AND streams.watts must NOT refetch")


class AggregatorShimTests(unittest.TestCase):
    """Test 12 — _aggregate_best_efforts_90d shim regression guard."""

    def test_shim_returns_dict_of_int_int(self):
        """The shim MUST return a dict {duration_s: watts} restricted to
        the original 4-tier set (180/300/600/1200) so legacy call sites
        (training_planner._project_event_capability) keep working.
        """
        import app
        out = app._aggregate_best_efforts_90d()
        self.assertIsInstance(out, dict)
        # Every key is in the locked tier set.
        self.assertTrue(set(out.keys()) <= {180, 300, 600, 1200},
                        f"shim returned out-of-tier keys: {set(out.keys())}")
        # Every value is int (or coercible).
        for v in out.values():
            self.assertIsInstance(v, int)


if __name__ == "__main__":
    unittest.main()
