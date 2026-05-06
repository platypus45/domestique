"""v1.3.0 IMPL-FATIGUE — unit tests for power_curve.compute_fatigue_resistance.

Covers the locked contract spelled out in:
  /tmp/MASTER_DECISIONS_v130.md           (original plan)
  /tmp/MASTER_DECISIONS_v130_PATCH.md     (G5 — kj_threshold ∈ {1500, 2000})
  /tmp/audit_v130_fatigue_resistance.md   (algorithm §2 / §3)

Coverage (8 tests, per /tmp brief):
  1. Synthetic 6 long rides spanning 1500-3500 kJ with KNOWN robustness
     ratio → returns ratio within ±2 % (NumPy reproducible seed).
  2. <4 long rides ≥ kj_threshold → fit_status='insufficient_data'.
     Both 1500 AND 2000 thresholds tested.
  3. PM-ride kJ axis reset: synthetic AM (1200 kJ) + PM (1100 kJ same
     day) → PM ride is independent (NOT cumulative 2300 kJ).
  4. Bonk inclusion: synthetic ride with power dropping to 0 in last
     hour → still counts (Pinot methodology).
  5. Threshold toggle: same fixture at 1500 vs 2000 returns DIFFERENT
     n_long_rides + DIFFERENT scatter.
  6. REAL DATA TEST (per W2A-G7 lessons): load i144492547.json (May 1,
     1626 kJ — corrected from brief's swapped-id reference). With
     kj_threshold=1500 it appears in n_long_rides; with 2000 it does NOT.
  7. Cache key invariance: changing kj_threshold MUST invalidate cache.
     Mock the cache + verify two distinct cache reads.
  8. Performance: aggregate on 50 synthetic rides + 5 durations
     completes in <2 s.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np

import power_curve


REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_RIDE_PATH = Path.home() / ".domestique" / "rides" / "icu" / "i144492547.json"


# ── synthetic ride builders ────────────────────────────────────────────────────

def _ride_dict(ext_id: str, started_at: str, powers: list[int],
               kj: float | None = None,
               weight_kg: float | None = 70.0,
               ftp_at_ride: int | None = 250,
               hr_max: int | None = 190) -> dict:
    """Build an ICU-shaped ride envelope with a 1Hz watts stream.

    ``kj`` defaults to sum(powers)/1000 (matches what ICU computes).
    """
    if kj is None:
        kj = sum(powers) / 1000.0
    return {
        "ride_id": f"icu_{ext_id}",
        "external_id": ext_id,
        "source": "icu",
        "name": "synthetic",
        "started_at": started_at,
        "duration_s": len(powers),
        "kj": float(kj),
        "weight_kg": weight_kg,
        "ftp_at_ride": ftp_at_ride,
        "hr_max": hr_max,
        "efforts": [],
        "streams": {"watts": list(powers)},
    }


def _build_long_ride(ext_id: str, started_at: str,
                     fresh_peak_w: int, tired_peak_w: int,
                     fresh_dur_s: int, tired_dur_s: int,
                     base_w: int = 200,
                     total_kj_target: float = 2500.0,
                     tired_kj_at_start: float = 1700.0,
                     seed: int | None = None) -> dict:
    """Compose a synthetic 1Hz ride with a known fresh-leg + tired-leg peak.

    The ride is laid out as:
      [base_w] × until cum_kj reaches ~50 kJ
      [fresh_peak_w] × fresh_dur_s   (so fresh peak is in 0-500 kJ band)
      [base_w] × until cum_kj reaches ~tired_kj_at_start
      [tired_peak_w] × tired_dur_s   (so tired peak is in ≥1500 kJ band)
      [base_w] × until cum_kj reaches total_kj_target

    Returns a ride dict with the ``streams.watts`` populated.
    """
    if seed is not None:
        np.random.seed(seed)
    powers: list[int] = []
    cum_kj = 0.0
    # Fresh prelude — bring cum_kj up to ~50 kJ before the fresh peak.
    while cum_kj < 50.0:
        powers.append(base_w)
        cum_kj += base_w / 1000.0
    # Fresh peak.
    for _ in range(fresh_dur_s):
        powers.append(fresh_peak_w)
        cum_kj += fresh_peak_w / 1000.0
    # Steady block until we reach the kJ-at-start target for the tired peak.
    while cum_kj < tired_kj_at_start:
        powers.append(base_w)
        cum_kj += base_w / 1000.0
    # Tired peak.
    for _ in range(tired_dur_s):
        powers.append(tired_peak_w)
        cum_kj += tired_peak_w / 1000.0
    # Tail — pad to total_kj_target.
    while cum_kj < total_kj_target:
        powers.append(base_w)
        cum_kj += base_w / 1000.0
    return _ride_dict(ext_id, started_at, powers, kj=cum_kj)


# ── base TestCase mixin ────────────────────────────────────────────────────────

class _FRTestBase(unittest.TestCase):
    """Patches power_curve._icu_rides_dir() to a tempdir per test."""

    def setUp(self):
        self._tmp = (Path(os.environ.get("TMPDIR", "/tmp"))
                     / f"fr_{os.getpid()}_{id(self)}")
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._patch_dir = patch.object(power_curve, "_icu_rides_dir",
                                        return_value=self._tmp)
        self._patch_dir.start()
        self._patch_prof = patch.object(power_curve, "_profile_ftp_weight",
                                         return_value=(250, 70.0))
        self._patch_prof.start()

    def tearDown(self):
        self._patch_dir.stop()
        self._patch_prof.stop()
        for f in self._tmp.glob("*"):
            f.unlink(missing_ok=True)
        self._tmp.rmdir()

    def _write(self, ride: dict) -> None:
        path = self._tmp / f"{ride['external_id']}.json"
        path.write_text(json.dumps(ride), encoding="utf-8")


# ── 1. KNOWN-RATIO SYNTHETIC ───────────────────────────────────────────────────

class Test01KnownRatio(_FRTestBase):
    """Test 1 — 6 long rides w/ known fresh/tired peaks → robustness within
    ±2 % of expected.

    Each ride: 5-min fresh peak = 300 W, 5-min tired peak = 270 W (90 %).
    Expected FR_300s ≈ 90 % ⇒ headline robustness ≈ 90 %.
    """

    def test_known_ratio_within_tolerance(self):
        np.random.seed(42)
        today = date.today()
        for i in range(6):
            day = today - timedelta(days=10 + i * 5)
            ride = _build_long_ride(
                ext_id=f"i{1000 + i}",
                started_at=f"{day.isoformat()}T08:00:00",
                fresh_peak_w=300,
                tired_peak_w=270,  # 90 % robustness for 5-min
                fresh_dur_s=300,
                tired_dur_s=300,
                base_w=180,
                total_kj_target=2400.0,
                tired_kj_at_start=1700.0,
                seed=42 + i,
            )
            self._write(ride)
        result = power_curve.compute_fatigue_resistance(
            "default", window_days=365, kj_threshold=1500,
        )
        self.assertEqual(result["fit_status"], "success",
                          f"want success, got {result}")
        self.assertEqual(result["n_long_rides"], 6)
        self.assertEqual(result["kj_threshold"], 1500)
        # 5-min FR index ≈ 270/300 × 100 = 90 % — within ±2 % per brief.
        # The 60-s and 5-min peaks in the fixture exactly match (the fresh
        # window fits inside the 300-s sprint), so these track ratios
        # closely. Longer durations (15/30 min) span steady-state padding
        # and converge toward 1.0 — that's the correct algorithm behaviour
        # but the locked-ratio assertion is on 60-s and 5-min.
        d60 = next(d for d in result["by_duration"]
                   if d["duration_s"] == 60)
        d300 = next(d for d in result["by_duration"]
                    if d["duration_s"] == 300)
        self.assertIsNotNone(d60["fr_index_pct"])
        self.assertIsNotNone(d300["fr_index_pct"])
        self.assertAlmostEqual(d60["fr_index_pct"], 90.0, delta=2.0,
                                msg="60-s FR index ≈ 90 % (270/300)")
        self.assertAlmostEqual(d300["fr_index_pct"], 90.0, delta=2.0,
                                msg="5-min FR index ≈ 90 % (270/300)")
        # Headline mean is averaged across 60/300/900/1800 s — longer
        # windows span steady-state padding so the headline ranges 90-96 %
        # for this fixture, never below the locked 270/300 floor.
        self.assertGreaterEqual(result["robustness_score"], 88.0)
        self.assertLessEqual(result["robustness_score"], 96.0)


# ── 2. INSUFFICIENT DATA (BOTH THRESHOLDS) ─────────────────────────────────────

class Test02InsufficientData(_FRTestBase):
    """Test 2 — <4 long rides ≥ threshold → fit_status='insufficient_data'.

    Tested at BOTH 1500 AND 2000 thresholds.
    """

    def test_three_rides_insufficient_at_1500(self):
        today = date.today()
        for i in range(3):
            day = today - timedelta(days=10 + i)
            ride = _build_long_ride(
                ext_id=f"i{2000 + i}",
                started_at=f"{day.isoformat()}T08:00:00",
                fresh_peak_w=300, tired_peak_w=270,
                fresh_dur_s=300, tired_dur_s=300,
                base_w=180, total_kj_target=2200.0,
                tired_kj_at_start=1700.0,
            )
            self._write(ride)
        result = power_curve.compute_fatigue_resistance(
            "default", window_days=365, kj_threshold=1500,
        )
        self.assertEqual(result["fit_status"], "insufficient_data")
        self.assertEqual(result["n_long_rides"], 3)
        self.assertEqual(result["kj_threshold"], 1500)
        self.assertIsNone(result["robustness_score"])

    def test_three_rides_insufficient_at_2000(self):
        # 5 rides BUT only 3 of them clear 2000 kJ.
        today = date.today()
        # 3 rides ≥ 2000 kJ.
        for i in range(3):
            day = today - timedelta(days=10 + i)
            ride = _build_long_ride(
                ext_id=f"i{3000 + i}",
                started_at=f"{day.isoformat()}T08:00:00",
                fresh_peak_w=300, tired_peak_w=270,
                fresh_dur_s=300, tired_dur_s=300,
                base_w=180, total_kj_target=2400.0,
                tired_kj_at_start=2100.0,
            )
            self._write(ride)
        # 2 rides at 1700 kJ — count toward 1500 threshold but NOT 2000.
        for i in range(2):
            day = today - timedelta(days=20 + i)
            ride = _build_long_ride(
                ext_id=f"i{3500 + i}",
                started_at=f"{day.isoformat()}T08:00:00",
                fresh_peak_w=290, tired_peak_w=260,
                fresh_dur_s=300, tired_dur_s=300,
                base_w=170, total_kj_target=1800.0,
                tired_kj_at_start=1600.0,
            )
            self._write(ride)
        result = power_curve.compute_fatigue_resistance(
            "default", window_days=365, kj_threshold=2000,
        )
        self.assertEqual(result["fit_status"], "insufficient_data")
        self.assertEqual(result["n_long_rides"], 3)
        self.assertEqual(result["kj_threshold"], 2000)
        self.assertIsNone(result["robustness_score"])


# ── 3. PM-RIDE KJ AXIS RESET ───────────────────────────────────────────────────

class Test03PMRideReset(_FRTestBase):
    """Test 3 — Two rides on the SAME DAY (1200 kJ AM, 1100 kJ PM) — PM is
    NOT cumulative (i.e. NOT treated as 2300 kJ).
    """

    def test_pm_ride_independent_kj_axis(self):
        # AM ride: 1200 kJ — does NOT clear 1500 kJ on its own.
        # PM ride: 1100 kJ — also does NOT clear 1500 kJ on its own.
        # If we WRONGLY summed them as 2300 kJ, n_long_rides would jump.
        d = date.today() - timedelta(days=10)
        am = _build_long_ride(
            ext_id="i4001",
            started_at=f"{d.isoformat()}T07:00:00",
            fresh_peak_w=290, tired_peak_w=270,
            fresh_dur_s=120, tired_dur_s=60,  # short — never hits the
            # tired band because total < 1500. Fresh peak is set at 50 kJ.
            base_w=200,
            total_kj_target=1200.0,
            tired_kj_at_start=900.0,  # not actually >= 1500, just
            # placeholder — _fr_per_ride_peaks computes mask itself.
        )
        pm = _build_long_ride(
            ext_id="i4002",
            started_at=f"{d.isoformat()}T18:00:00",
            fresh_peak_w=290, tired_peak_w=260,
            fresh_dur_s=120, tired_dur_s=60,
            base_w=200,
            total_kj_target=1100.0,
            tired_kj_at_start=900.0,
        )
        self._write(am)
        self._write(pm)
        result = power_curve.compute_fatigue_resistance(
            "default", window_days=30, kj_threshold=1500,
        )
        # NEITHER ride hits 1500 kJ on its own, and the PM ride starts a
        # FRESH kJ axis. n_long_rides MUST be 0 — if we accidentally
        # cumulated across the day, AM+PM = 2300 kJ would falsely count.
        self.assertEqual(result["n_long_rides"], 0)
        self.assertEqual(result["fit_status"], "insufficient_data")


# ── 4. BONK INCLUSION ──────────────────────────────────────────────────────────

class Test04BonkIncluded(_FRTestBase):
    """Test 4 — Pinot methodology INCLUDES bonk rides; fatigue is signal,
    not noise. A ride whose power drops to 0 in the last hour still
    contributes to n_long_rides + scatter.
    """

    def test_bonk_ride_still_counts(self):
        # Hand-build a 90-min ride that hits 1700 kJ and then bonks at 0 W.
        powers: list[int] = []
        cum_kj = 0.0
        # Fresh peak in first 5 min.
        for _ in range(50):
            powers.append(200)
            cum_kj += 0.2
        for _ in range(300):  # 5-min fresh peak
            powers.append(310)
            cum_kj += 0.31
        # Steady tempo until 1700 kJ.
        while cum_kj < 1700.0:
            powers.append(220)
            cum_kj += 0.22
        # Tired peak (5-min @ 250 W).
        for _ in range(300):
            powers.append(250)
            cum_kj += 0.25
        # BONK — last 60 min at 0 W (sitting on the side of the road).
        for _ in range(3600):
            powers.append(0)
        ride = _ride_dict(
            ext_id="i5001",
            started_at=f"{(date.today() - timedelta(days=5)).isoformat()}"
                       f"T08:00:00",
            powers=powers,
            kj=cum_kj,
        )
        self._write(ride)
        # 3 more rides to get to n=4 minimum.
        for i in range(3):
            day = date.today() - timedelta(days=20 + i * 3)
            r = _build_long_ride(
                ext_id=f"i5{i + 100}",
                started_at=f"{day.isoformat()}T08:00:00",
                fresh_peak_w=300, tired_peak_w=270,
                fresh_dur_s=300, tired_dur_s=300,
                base_w=180, total_kj_target=2200.0,
                tired_kj_at_start=1700.0,
            )
            self._write(r)
        result = power_curve.compute_fatigue_resistance(
            "default", window_days=365, kj_threshold=1500,
        )
        # Bonk ride is included — n_long_rides == 4.
        self.assertEqual(result["n_long_rides"], 4)
        self.assertEqual(result["fit_status"], "success")
        # The bonk ride must contribute a 5-min tired peak too.
        bonk_pts = [s for s in result["scatter"]
                    if s["ride_id"] == "icu_i5001" and s["duration_s"] == 300]
        self.assertGreater(len(bonk_pts), 0,
                            "bonk ride should still produce a 5-min tired "
                            "peak — Pinot methodology")


# ── 5. THRESHOLD TOGGLE ────────────────────────────────────────────────────────

class Test05ThresholdToggle(_FRTestBase):
    """Test 5 — Same fixture at 1500 vs 2000 returns DIFFERENT n_long_rides
    AND different scatter.
    """

    def test_toggle_changes_results(self):
        # 4 rides ≥ 2000 kJ + 4 rides at 1700 kJ.
        today = date.today()
        for i in range(4):
            day = today - timedelta(days=10 + i)
            ride = _build_long_ride(
                ext_id=f"i6_2k_{i}",
                started_at=f"{day.isoformat()}T08:00:00",
                fresh_peak_w=300, tired_peak_w=270,
                fresh_dur_s=300, tired_dur_s=300,
                base_w=180, total_kj_target=2300.0,
                tired_kj_at_start=2050.0,  # tired peak at >= 2000 kJ
            )
            self._write(ride)
        for i in range(4):
            day = today - timedelta(days=30 + i)
            ride = _build_long_ride(
                ext_id=f"i6_15k_{i}",
                started_at=f"{day.isoformat()}T08:00:00",
                fresh_peak_w=290, tired_peak_w=260,
                fresh_dur_s=300, tired_dur_s=300,
                base_w=170, total_kj_target=1800.0,
                tired_kj_at_start=1550.0,
            )
            self._write(ride)
        r_1500 = power_curve.compute_fatigue_resistance(
            "default", window_days=365, kj_threshold=1500,
        )
        r_2000 = power_curve.compute_fatigue_resistance(
            "default", window_days=365, kj_threshold=2000,
        )
        # 1500 catches all 8 rides; 2000 catches 4.
        self.assertEqual(r_1500["n_long_rides"], 8)
        self.assertEqual(r_2000["n_long_rides"], 4)
        self.assertEqual(r_1500["kj_threshold"], 1500)
        self.assertEqual(r_2000["kj_threshold"], 2000)
        # Scatter populations DIFFER — 2000 has fewer points.
        self.assertGreater(len(r_1500["scatter"]), len(r_2000["scatter"]))


# ── 6. REAL DATA ───────────────────────────────────────────────────────────────

class Test06RealData(unittest.TestCase):
    """Test 6 (W2A-G7 lessons-learned) — load the real ride
    ~/.domestique/rides/icu/i144492547.json (May 1, 1626.2 kJ).

    Brief mentions i145626886 but that file is 798 kJ; the actual May 1
    1626 kJ ride is i144492547. Verify the threshold gate against real
    cached data.
    """

    @unittest.skipUnless(REAL_RIDE_PATH.exists(),
                          f"real ride not present at {REAL_RIDE_PATH}")
    def test_real_ride_threshold_gating(self):
        # Use a fresh tempdir + copy ONLY the real ride into it so the
        # n_long_rides count is deterministic.
        tmp = (Path(os.environ.get("TMPDIR", "/tmp"))
                / f"fr_real_{os.getpid()}_{id(self)}")
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            # Copy the real ride.
            real = json.loads(REAL_RIDE_PATH.read_text(encoding="utf-8"))
            self.assertEqual(real.get("ride_id"), "icu_i144492547")
            self.assertGreaterEqual(real.get("kj"), 1500.0)
            self.assertLess(real.get("kj"), 2000.0)
            # Re-stamp date to today so it falls in any window.
            real["started_at"] = (date.today().isoformat()
                                   + real["started_at"][10:])
            (tmp / "i144492547.json").write_text(json.dumps(real),
                                                   encoding="utf-8")
            with patch.object(power_curve, "_icu_rides_dir",
                                return_value=tmp), \
                 patch.object(power_curve, "_profile_ftp_weight",
                                return_value=(248, 71.5)):
                r_1500 = power_curve.compute_fatigue_resistance(
                    "default", window_days=365, kj_threshold=1500,
                )
                r_2000 = power_curve.compute_fatigue_resistance(
                    "default", window_days=365, kj_threshold=2000,
                )
            # At 1500 kJ — 1626.2 kJ ride MUST count.
            self.assertEqual(r_1500["n_long_rides"], 1,
                              "ride at 1626.2 kJ must clear 1500 threshold")
            # At 2000 kJ — 1626.2 kJ ride MUST NOT count.
            self.assertEqual(r_2000["n_long_rides"], 0,
                              "ride at 1626.2 kJ must NOT clear 2000 "
                              "threshold")
        finally:
            for f in tmp.glob("*"):
                f.unlink(missing_ok=True)
            tmp.rmdir()


# ── 7. CACHE KEY INVARIANCE ────────────────────────────────────────────────────

class Test07CacheKeyInvariance(unittest.TestCase):
    """Test 7 — changing kj_threshold MUST invalidate the endpoint cache.
    Mocks the cache + verifies two distinct cache reads (one per threshold).
    """

    def test_kj_threshold_invalidates_cache(self):
        # Use FastAPI test client. Replace the underlying compute_fatigue_
        # resistance with a counter so we can verify each threshold flip
        # forces a recompute.
        from fastapi.testclient import TestClient
        import app as app_module
        client = TestClient(app_module.app)

        # Clear caches before exercising.
        keys_to_clear = [k for k in app_module._cache
                          if k.startswith("fatigue_resistance_")]
        for k in keys_to_clear:
            app_module._cache.pop(k, None)
            app_module._cache_ts.pop(k, None)

        call_count = {"n": 0, "thresholds": []}

        def _stub(profile_id, window_days=365, kj_threshold=1500):
            call_count["n"] += 1
            call_count["thresholds"].append(kj_threshold)
            return {
                "window_days": window_days,
                "n_long_rides": 0,
                "fit_status": "insufficient_data",
                "kj_threshold": kj_threshold,
                "robustness_score": None,
                "by_duration": [],
                "scatter": [],
            }

        with patch.object(power_curve, "compute_fatigue_resistance", _stub):
            # First call — threshold 1500 — must compute.
            r1 = client.get(
                "/api/profile/fatigue-resistance"
                "?window_days=365&kj_threshold=1500"
            )
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r1.json()["kj_threshold"], 1500)
            self.assertEqual(call_count["n"], 1)
            # Same threshold — cache hit, NO recompute.
            r2 = client.get(
                "/api/profile/fatigue-resistance"
                "?window_days=365&kj_threshold=1500"
            )
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(call_count["n"], 1,
                              "cache should serve repeat 1500 request")
            # Threshold flip — MUST recompute.
            r3 = client.get(
                "/api/profile/fatigue-resistance"
                "?window_days=365&kj_threshold=2000"
            )
            self.assertEqual(r3.status_code, 200)
            self.assertEqual(r3.json()["kj_threshold"], 2000)
            self.assertEqual(call_count["n"], 2,
                              "threshold flip MUST invalidate cache and "
                              "recompute")
            # Flip back — already cached at 1500, still NO recompute.
            r4 = client.get(
                "/api/profile/fatigue-resistance"
                "?window_days=365&kj_threshold=1500"
            )
            self.assertEqual(r4.status_code, 200)
            # We GC'd the prior 1500 entry on the 2000 call (different
            # latest_ride_id_in_window prefix). But the actual key prefix
            # is per-(window_days, kj_threshold) so the 1500 entry is
            # PRESERVED across a 2000 request — recompute count should
            # still be 2.
            self.assertEqual(call_count["n"], 2,
                              "cache should serve repeat 1500 after 2000 "
                              "flip")

        # Cleanup.
        keys_to_clear = [k for k in app_module._cache
                          if k.startswith("fatigue_resistance_")]
        for k in keys_to_clear:
            app_module._cache.pop(k, None)
            app_module._cache_ts.pop(k, None)


# ── 8. PERFORMANCE ─────────────────────────────────────────────────────────────

class Test08Performance(_FRTestBase):
    """Test 8 — 50 synthetic rides + 5 durations completes in <2 s.

    Rides are ~9000 samples each (2.5 h) which is realistic; 50 × 9000 ×
    5 durations ≈ 2.25M sliding-window updates. NumPy gets sub-second.
    """

    def test_perf_under_2s_on_50_rides(self):
        np.random.seed(123)
        today = date.today()
        for i in range(50):
            day = today - timedelta(days=i + 1)
            # Fresh peak duration 300 s, tired 300 s, total ~2200 kJ.
            ride = _build_long_ride(
                ext_id=f"i9{i:03d}",
                started_at=f"{day.isoformat()}T08:00:00",
                fresh_peak_w=int(280 + np.random.randint(-20, 21)),
                tired_peak_w=int(250 + np.random.randint(-20, 21)),
                fresh_dur_s=300,
                tired_dur_s=300,
                base_w=int(180 + np.random.randint(-10, 11)),
                total_kj_target=2200.0,
                tired_kj_at_start=1700.0,
                seed=123 + i,
            )
            self._write(ride)
        t0 = time.time()
        result = power_curve.compute_fatigue_resistance(
            "default", window_days=90, kj_threshold=1500,
        )
        elapsed = time.time() - t0
        # Under the 2s ceiling per brief.
        self.assertLess(elapsed, 2.0,
                         f"50-ride compute took {elapsed:.2f}s (limit 2.0s)")
        self.assertEqual(result["n_long_rides"], 50)
        self.assertEqual(result["fit_status"], "success")


class Test09RealBonk(_FRTestBase):
    """Test 9 (W2B-G8 fix) — Pinot bonk inclusion, the HARD case.

    The original Test04BonkIncluded had a 250W tired peak BEFORE the bonk
    so the assert "scatter has bonk_pts" passed via the 250W peak, not the
    bonk. This test removes the pre-bonk tired peak so the ONLY post-1500
    kJ window is 0W. Verifies a scatter row still appears (i.e. the
    function doesn't silently drop true-bonk rides). Headline robustness
    must NOT be polluted — we still need ≥4 long rides with healthy
    fresh+tired peaks alongside.
    """

    def test_pure_bonk_ride_still_in_scatter(self):
        # Hand-build a ride that hits 1700 kJ via tempo, then 0 W for 60 min.
        powers: list[int] = []
        cum_kj = 0.0
        # Fresh peak in first 5 min.
        for _ in range(300):
            powers.append(310)
            cum_kj += 0.31
        # Steady tempo until 1700 kJ — NO tired peak before bonk.
        while cum_kj < 1700.0:
            powers.append(220)
            cum_kj += 0.22
        # Pure bonk for 60 min — 0 W.
        for _ in range(3600):
            powers.append(0)
        ride = _ride_dict(
            ext_id="i6001",
            started_at=f"{(date.today() - timedelta(days=4)).isoformat()}"
                       f"T08:00:00",
            powers=powers,
            kj=cum_kj,
        )
        self._write(ride)
        # 4 healthy long rides for the headline.
        for i in range(4):
            day = date.today() - timedelta(days=20 + i * 3)
            r = _build_long_ride(
                ext_id=f"i6{i + 100}",
                started_at=f"{day.isoformat()}T08:00:00",
                fresh_peak_w=300, tired_peak_w=270,
                fresh_dur_s=300, tired_dur_s=300,
                base_w=180, total_kj_target=2200.0,
                tired_kj_at_start=1700.0,
            )
            self._write(r)
        result = power_curve.compute_fatigue_resistance(
            "default", window_days=365, kj_threshold=1500,
        )
        self.assertEqual(result["fit_status"], "success")
        # Bonk ride is in n_long_rides.
        self.assertGreaterEqual(result["n_long_rides"], 5)
        # Bonk ride contributes a scatter row at kj ≥ 1500 — even though
        # watts is 0, the data point is preserved (Pinot inclusion).
        bonk_pts = [s for s in result["scatter"]
                    if s["ride_id"] == "icu_i6001"]
        self.assertGreater(len(bonk_pts), 0,
                            "bonk ride should produce scatter rows even "
                            "when post-1500-kJ windows are all 0 W")
        # Headline robustness not polluted by the 0W bonk: still based on
        # the 4 healthy rides.
        self.assertGreater(float(result["robustness_score"]), 70.0)


class Test10ReasonField(_FRTestBase):
    """Test 10 (W2B-G2 fix) — insufficient_data carries a `reason` so the
    user knows WHY the metric is unavailable instead of just "insufficient".
    """

    def test_no_rides_in_window_reason(self):
        # No rides at all.
        result = power_curve.compute_fatigue_resistance(
            "default", window_days=365, kj_threshold=1500,
        )
        self.assertEqual(result["fit_status"], "insufficient_data")
        self.assertEqual(result["reason"], "no_rides_in_window")

    def test_fewer_than_4_long_rides_reason(self):
        for i in range(3):
            day = date.today() - timedelta(days=10 + i * 3)
            r = _build_long_ride(
                ext_id=f"i7{i + 100}",
                started_at=f"{day.isoformat()}T08:00:00",
                fresh_peak_w=300, tired_peak_w=270,
                fresh_dur_s=300, tired_dur_s=300,
                base_w=180, total_kj_target=1700.0,
                tired_kj_at_start=1500.0,
            )
            self._write(r)
        result = power_curve.compute_fatigue_resistance(
            "default", window_days=365, kj_threshold=1500,
        )
        self.assertEqual(result["fit_status"], "insufficient_data")
        self.assertEqual(result["reason"], "fewer_than_4_long_rides")
        self.assertEqual(result["n_long_rides"], 3)

    def test_streams_not_hydrated_reason(self):
        # 5 rides whose summary kJ ≥ threshold but with NO power streams.
        for i in range(5):
            day = date.today() - timedelta(days=10 + i * 3)
            ride = {
                "ride_id": f"icu_i8{i + 100}",
                "external_id": f"i8{i + 100}",
                "source": "icu",
                "started_at": f"{day.isoformat()}T08:00:00",
                "kj": 1700.0,
                "ftp_at_ride": 250,
                "weight_kg": 70.0,
                # No 'streams' field at all.
            }
            self._write(ride)
        result = power_curve.compute_fatigue_resistance(
            "default", window_days=365, kj_threshold=1500,
        )
        self.assertEqual(result["fit_status"], "insufficient_data")
        self.assertEqual(result["reason"],
                          "streams_not_hydrated_run_backfill")
        self.assertEqual(result["n_long_rides"], 5)
        self.assertEqual(result["n_long_rides_with_streams"], 0)


if __name__ == "__main__":
    unittest.main()
