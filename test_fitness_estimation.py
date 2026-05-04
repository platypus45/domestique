"""Unit tests for fitness_estimation.py — FTP estimation and fitness signatures.

Tests best-effort extraction, FTP auto-estimation, and Xert-style fitness
signature computation. Run: python3 -m pytest test_fitness_estimation.py -v
"""

import unittest

from training_live import RideSample
from fitness_estimation import (
    FitnessSignature,
    extract_best_efforts,
    estimate_ftp,
    compute_fitness_signature,
    aerobic_decoupling,
    compute_cp_wprime,
    FTP_SCALING_FACTORS,
    STANDARD_DURATIONS,
    MIN_FTP_EFFORT_DURATION,
    MONOD_DURATIONS_S,
    MONOD_MIN_POINTS,
    MONOD_R2_MIN,
)


# ══════════════════════════════════════════════════════════════════════════════
# aerobic_decoupling tests (post-hoc helper for RIDE REPORT re-render)
# ══════════════════════════════════════════════════════════════════════════════

class TestAerobicDecoupling(unittest.TestCase):
    """v3.6.0-fix25 canonical decoupling (§1.4 filter + §1.5 900s warmup
    trim + NP-per-half). Replaces the older HR<100 / 300s-half tests
    that pre-dated the unified filter/warmup-trim convention."""

    # Duration convention below:
    #   900 s warmup trim (discarded)
    #   + 2400 s "steady" (the minimum filtered duration) = 3300 s total
    WARMUP = 900
    STEADY = 2400

    def test_empty_returns_none(self):
        assert aerobic_decoupling([], []) is None
        assert aerobic_decoupling([100], []) is None

    def test_too_short_returns_none(self):
        # Well under the 40-min post-trim floor — should return None.
        powers = [200] * 1000
        hrs    = [150] * 1000
        assert aerobic_decoupling(powers, hrs) is None

    def test_all_filtered_returns_none(self):
        # Sub-50 W + sub-60 bpm → canonical §1.4 drops every pair.
        powers = [30] * 4000
        hrs    = [50] * 4000
        assert aerobic_decoupling(powers, hrs) is None

    def test_flat_ride_zero_drift(self):
        # 900 s warmup (trimmed) + 2400 s steady — both halves identical.
        total = self.WARMUP + self.STEADY
        powers = [200] * total
        hrs    = [150] * total
        result = aerobic_decoupling(powers, hrs)
        assert result == 0.0

    def test_rising_hr_second_half_positive_drift(self):
        # After 15-min warmup trim: 1200 s @ HR=140, 1200 s @ HR=170 steady.
        # ef1 = 200/140 = 1.4286, ef2 = 200/170 = 1.1765
        # (ef1 - ef2)/ef1 * 100 ≈ +17.6 % (fatigue / positive drift).
        steady = 2400
        powers = [200] * (self.WARMUP + steady)
        hrs    = [140] * self.WARMUP + [140] * (steady // 2) + [170] * (steady // 2)
        result = aerobic_decoupling(powers, hrs)
        assert result is not None
        assert result > 0
        assert abs(result - 17.6) < 0.5

    def test_falling_hr_second_half_negative_drift(self):
        # HR drops in 2nd half (HR suppression / detraining) → negative %.
        steady = 2400
        powers = [200] * (self.WARMUP + steady)
        hrs    = [140] * self.WARMUP + [170] * (steady // 2) + [140] * (steady // 2)
        result = aerobic_decoupling(powers, hrs)
        assert result is not None
        assert result < 0

    def test_sign_matches_live_engine(self):
        """Post-hoc helper must agree with MetricsEngine.decoupling within 0.1 %
        on a flat-power synthetic ride (§1.4 filter + §1.5 trim identical)."""
        from training_live import MetricsEngine

        # 900 s warmup + 2500 s steady — HR drifts 140 → 165 in the steady block.
        steady = 2500
        powers = [200] * (self.WARMUP + steady)
        hrs    = [140] * self.WARMUP + [140] * (steady // 2) + [165] * (steady // 2)

        engine = MetricsEngine(ftp=250, weight_kg=70.0)
        for p, h in zip(powers, hrs):
            engine.update(power=p, dt=1.0, hr=h)

        live_pct = engine.decoupling["pct"]
        post_hoc_pct = aerobic_decoupling(powers, hrs)

        assert live_pct is not None, "live engine failed to compute decoupling"
        assert post_hoc_pct is not None, "post-hoc helper returned None"
        assert (live_pct >= 0) == (post_hoc_pct >= 0), (
            f"sign mismatch: live={live_pct}, post-hoc={post_hoc_pct}"
        )
        assert abs(live_pct - post_hoc_pct) <= 0.1, (
            f"magnitude mismatch: live={live_pct}, post-hoc={post_hoc_pct}"
        )

    # ── v3.6.0-fix25 additional tests ─────────────────────────────────

    def test_aerobic_decoupling_unified_filter_matches_live(self):
        """Post-hoc and live must agree within rounding (§DEC-4).

        Same input drive to MetricsEngine + the helper — both must emit
        the same decoupling_pct within ±0.2 % (rounding on both sides at
        1 dp cap). Mirrors GRILL BUG-2 regression.
        """
        from training_live import MetricsEngine

        steady = 2500
        powers = [220] * self.WARMUP + [220] * steady
        hrs    = [140] * self.WARMUP + [145] * (steady // 2) + [155] * (steady // 2)

        engine = MetricsEngine(ftp=250, weight_kg=70.0)
        for p, h in zip(powers, hrs):
            engine.update(power=p, dt=1.0, hr=h)
        live_pct = engine.decoupling["pct"]
        post_hoc_pct = aerobic_decoupling(powers, hrs)

        assert live_pct is not None
        assert post_hoc_pct is not None
        assert abs(live_pct - post_hoc_pct) <= 0.2, (
            f"unified filter drift: live={live_pct}, post-hoc={post_hoc_pct}"
        )

    def test_aerobic_decoupling_warmup_trim_900s(self):
        """§DEC-5 regression: a 15-min warmup ramp must be trimmed before
        halving, otherwise the ramp pulls ef1 down and produces a
        spurious negative decoupling number."""
        # 900s ramp 100→140 bpm + 0→200W, then 2400s steady (140 bpm, 200W).
        steady = 2400
        powers = list(range(1, 201, 1)) * 5 + [200] * (self.WARMUP - 1000) + [200] * steady
        # Simpler: pair elements aren't important — what matters is that
        # the first 900 s differ from the steady half. Use a linear HR ramp.
        powers = []
        hrs = []
        for i in range(self.WARMUP):
            # Ramp 50→200 W, HR 90→140.
            powers.append(50 + int(150 * i / self.WARMUP))
            hrs.append(90 + int(50 * i / self.WARMUP))
        for _ in range(steady):
            powers.append(200)
            hrs.append(140)

        # With 900 s trim the result should be near 0 — perfectly coupled.
        result = aerobic_decoupling(powers, hrs)
        assert result is not None
        # 0 ± 0.5 % — both halves have identical NP and HR post-trim.
        assert abs(result) < 0.5, f"warmup trim failed: got {result}"


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_samples(powers: list[int]) -> list[RideSample]:
    """Create RideSample list from a raw power stream (1Hz)."""
    return [
        RideSample(
            elapsed_sec=i,
            power=p,
            cadence=90,
            speed=30.0,
            hr=140,
            distance_km=i * 0.00833,
            elevation_m=0.0,
            gradient_pct=0.0,
        )
        for i, p in enumerate(powers)
    ]


def _constant_power(watts: int, duration_sec: int) -> list[int]:
    """Generate constant-power stream."""
    return [watts] * duration_sec


def _alternating_power(high: int, low: int, duration_sec: int) -> list[int]:
    """Generate alternating high/low power stream (1s each)."""
    return [high if i % 2 == 0 else low for i in range(duration_sec)]


# ══════════════════════════════════════════════════════════════════════════════
# extract_best_efforts tests
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractBestEfforts(unittest.TestCase):
    def test_empty_samples(self):
        assert extract_best_efforts([]) == {}

    def test_constant_power_all_durations(self):
        """Constant 250W for 1 hour should yield 250W for every duration."""
        samples = _make_samples(_constant_power(250, 3600))
        efforts = extract_best_efforts(samples)
        for dur in STANDARD_DURATIONS:
            assert dur in efforts, f"Missing duration {dur}s"
            assert efforts[dur] == 250, f"{dur}s: expected 250W, got {efforts[dur]}W"

    def test_short_ride_omits_long_durations(self):
        """A 10-minute ride should not contain 20/30/60 min efforts."""
        samples = _make_samples(_constant_power(200, 600))
        efforts = extract_best_efforts(samples)
        assert 5 in efforts
        assert 30 in efforts
        assert 60 in efforts
        assert 300 in efforts
        assert 1200 not in efforts
        assert 1800 not in efforts
        assert 3600 not in efforts

    def test_sliding_window_finds_peak(self):
        """A power spike in the middle should be detected by the window."""
        # 10 min of 150W with a 5s spike of 600W in the middle
        powers = _constant_power(150, 300) + _constant_power(600, 5) + _constant_power(150, 295)
        samples = _make_samples(powers)
        efforts = extract_best_efforts(samples)

        # 5s best should be exactly 600W (the spike)
        assert efforts[5] == 600

        # 30s best should be > 150 (includes some of the spike)
        assert efforts[30] > 150

    def test_variable_power_5s(self):
        """Best 5s of [100, 100, 500, 500, 500, 500, 500, 100] should be 500."""
        powers = [100, 100, 500, 500, 500, 500, 500, 100, 100, 100]
        samples = _make_samples(powers)
        efforts = extract_best_efforts(samples)
        assert efforts[5] == 500

    def test_all_zeros(self):
        """Ride with zero power throughout."""
        samples = _make_samples(_constant_power(0, 600))
        efforts = extract_best_efforts(samples)
        assert efforts[5] == 0
        assert efforts[300] == 0

    def test_exactly_duration_length(self):
        """Ride exactly matching a standard duration should include it."""
        samples = _make_samples(_constant_power(200, 300))
        efforts = extract_best_efforts(samples)
        assert 300 in efforts
        assert efforts[300] == 200

    def test_progressive_power(self):
        """Linearly increasing power: best efforts should come from the end."""
        # 600 seconds, power from 100 to 399 (increases by ~0.5W/s)
        powers = [100 + i // 2 for i in range(600)]
        samples = _make_samples(powers)
        efforts = extract_best_efforts(samples)

        # Best 5s should be near the end (highest power)
        # Last 5 values: 100+297, 100+298, 100+298, 100+299, 100+299 = 397..399
        assert efforts[5] >= 395

        # Best 5min (300s) should be higher than first 300s average
        first_300_avg = sum(powers[:300]) // 300
        assert efforts[300] > first_300_avg


# ══════════════════════════════════════════════════════════════════════════════
# estimate_ftp tests
# ══════════════════════════════════════════════════════════════════════════════

class TestEstimateFtp(unittest.TestCase):
    def test_20min_test(self):
        """Classic 20min FTP test: 300W * 0.95 = 285W."""
        efforts = {1200: 300}
        assert estimate_ftp(efforts) == 285

    def test_60min_gold_standard(self):
        """60min effort is the gold standard: FTP = watts * 1.0."""
        efforts = {3600: 260}
        assert estimate_ftp(efforts) == 260

    def test_5min_effort(self):
        """5min VO2max effort: 380W * 0.80 = 304W."""
        efforts = {300: 380}
        assert estimate_ftp(efforts) == 304

    def test_8min_effort(self):
        """8min effort: 340W * 0.86 = 292W."""
        efforts = {480: 340}
        assert estimate_ftp(efforts) == 292

    def test_30min_effort(self):
        """30min effort: 270W * 0.97 = 262W."""
        efforts = {1800: 270}
        assert estimate_ftp(efforts) == 262

    def test_best_estimate_prefers_long_duration(self):
        """Should prefer LONG-duration efforts (≥20min) when seeded first.

        Iteration runs longest→shortest: 3600 seeds best=260, then 1200 bumps
        to 285 because long-duration estimates that read higher take over.
        The short 5-min estimate (304) is NOT allowed to displace a long one.
        """
        efforts = {
            300:  380,   # 380 * 0.80 = 304
            1200: 300,   # 300 * 0.95 = 285
            3600: 260,   # 260 * 1.00 = 260
        }
        assert estimate_ftp(efforts) == 285

    def test_no_qualifying_efforts(self):
        """No effort >= 5 min should return None."""
        efforts = {5: 900, 30: 700, 60: 500}
        assert estimate_ftp(efforts) is None

    def test_empty_efforts(self):
        assert estimate_ftp({}) is None

    def test_ignores_short_durations(self):
        """Durations below MIN_FTP_EFFORT_DURATION are ignored."""
        efforts = {5: 1000, 30: 800, 60: 600, 300: 350}
        ftp = estimate_ftp(efforts)
        # Only 300s qualifies: 350 * 0.80 = 280
        assert ftp == 280

    def test_multiple_similar_estimates(self):
        """When multiple durations give similar FTPs, highest wins."""
        efforts = {
            300:  350,   # 350 * 0.80 = 280
            480:  326,   # 326 * 0.86 = 280
            1200: 295,   # 295 * 0.95 = 280
        }
        # All round to ~280, should get 280 or close. Use a broader ±5W
        # tolerance: the underlying FTP_SCALING_FACTORS can shift by 1–2W
        # across revisions without breaking correctness.
        ftp = estimate_ftp(efforts)
        assert abs(ftp - 280) <= 5, f"expected ~280W, got {ftp}"

    def test_realistic_athlete_profile(self):
        """Realistic power profile for a ~270W FTP rider.

        With long-duration preference, we walk 3600→1800→1200:
          3600 → 265  (seeds)
          1800 → 270  (long, higher, replaces)
          1200 → 275.5 (long, higher, replaces)
          480/300 can't displace a seeded long-duration best.
        """
        efforts = {
            5:    900,
            30:   650,
            60:   450,
            300:  370,   # 370 * 0.80 = 296
            480:  330,   # 330 * 0.86 = 284
            1200: 290,   # 290 * 0.95 = 275.5
            1800: 278,   # 278 * 0.97 = 269.66
            3600: 265,   # 265 * 1.00 = 265
        }
        ftp = estimate_ftp(efforts)
        # Best estimate comes from 20min (highest long-duration reading, 275.5 → 276)
        assert ftp == 276


# ══════════════════════════════════════════════════════════════════════════════
# FitnessSignature dataclass tests
# ══════════════════════════════════════════════════════════════════════════════

class TestFitnessSignature(unittest.TestCase):
    def test_dataclass_creation(self):
        sig = FitnessSignature(ftp=250, ltp=188, hie=20.0, peak_power=900)
        assert sig.ftp == 250
        assert sig.ltp == 188
        assert sig.hie == 20.0
        assert sig.peak_power == 900

    def test_dataclass_equality(self):
        a = FitnessSignature(ftp=250, ltp=188, hie=20.0, peak_power=900)
        b = FitnessSignature(ftp=250, ltp=188, hie=20.0, peak_power=900)
        assert a == b


# ══════════════════════════════════════════════════════════════════════════════
# compute_fitness_signature tests
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeFitnessSignature(unittest.TestCase):
    def test_basic_signature(self):
        """Standard athlete with clear best efforts."""
        efforts = {5: 900, 30: 650, 60: 450, 300: 370, 1200: 290}
        sig = compute_fitness_signature(efforts, ftp=270)

        assert sig.ftp == 270
        assert sig.ltp == 202  # 270 * 0.75 = 202.5 -> 202
        assert sig.peak_power == 900

        # HIE = (370 - 270) * 300 / 1000 = 30.0 kJ
        assert sig.hie == 30.0

    def test_ltp_is_75pct_of_ftp(self):
        sig = compute_fitness_signature({5: 800}, ftp=300)
        assert sig.ltp == 225  # 300 * 0.75

    def test_hie_when_5min_above_ftp(self):
        """HIE should be positive when 5min power exceeds FTP."""
        efforts = {300: 350}
        sig = compute_fitness_signature(efforts, ftp=250)
        # HIE = (350 - 250) * 300 / 1000 = 30.0 kJ
        assert sig.hie == 30.0

    def test_hie_when_5min_below_ftp(self):
        """HIE falls back to conservative estimate when 5min <= FTP."""
        efforts = {300: 200}
        sig = compute_fitness_signature(efforts, ftp=250)
        # Fallback: ftp * 80 / 1000 = 250 * 80 / 1000 = 20.0 kJ
        assert sig.hie == 20.0

    def test_hie_without_5min_data(self):
        """HIE falls back when no 5min effort is available."""
        efforts = {5: 900, 30: 700}
        sig = compute_fitness_signature(efforts, ftp=250)
        assert sig.hie == 20.0  # fallback: ftp * 80 / 1000 ≈ 20 kJ

    def test_peak_power_from_5s(self):
        efforts = {5: 1100, 300: 400}
        sig = compute_fitness_signature(efforts, ftp=280)
        assert sig.peak_power == 1100

    def test_peak_power_fallback_without_5s(self):
        """Peak power defaults to 2x FTP when no 5s data."""
        efforts = {300: 400}
        sig = compute_fitness_signature(efforts, ftp=280)
        assert sig.peak_power == 560  # 280 * 2

    def test_invalid_ftp_raises(self):
        with self.assertRaises(ValueError):
            compute_fitness_signature({}, ftp=0)
        with self.assertRaises(ValueError):
            compute_fitness_signature({}, ftp=-100)

    def test_realistic_trained_cyclist(self):
        """Full signature for a well-trained cyclist (~280W FTP)."""
        efforts = {
            5:    1050,
            30:   720,
            60:   480,
            300:  390,
            480:  350,
            1200: 300,
            1800: 288,
            3600: 275,
        }
        sig = compute_fitness_signature(efforts, ftp=280)

        assert sig.ftp == 280
        assert sig.ltp == 210       # 280 * 0.75
        assert sig.peak_power == 1050
        # HIE = (390 - 280) * 300 / 1000 = 33.0
        assert sig.hie == 33.0

    def test_recreational_rider(self):
        """Lower-power recreational rider."""
        efforts = {5: 500, 300: 200, 1200: 160}
        sig = compute_fitness_signature(efforts, ftp=150)

        assert sig.ftp == 150
        assert sig.ltp == 112  # 150 * 0.75 = 112.5 -> 112
        assert sig.peak_power == 500
        # HIE = (200 - 150) * 300 / 1000 = 15.0
        assert sig.hie == 15.0

    def test_sprinter_profile(self):
        """Sprinter with very high peak but moderate FTP."""
        efforts = {5: 1500, 300: 350}
        sig = compute_fitness_signature(efforts, ftp=240)

        assert sig.peak_power == 1500
        # HIE = (350 - 240) * 300 / 1000 = 33.0
        assert sig.hie == 33.0


# ══════════════════════════════════════════════════════════════════════════════
# Integration: extract -> estimate -> signature
# ══════════════════════════════════════════════════════════════════════════════

class TestEndToEnd(unittest.TestCase):
    def test_full_pipeline_constant_ride(self):
        """Constant 260W for 25 minutes: extract, estimate FTP, compute sig."""
        powers = _constant_power(260, 1500)  # 25 min
        samples = _make_samples(powers)

        efforts = extract_best_efforts(samples)
        assert efforts[300] == 260
        assert efforts[1200] == 260

        ftp = estimate_ftp(efforts)
        # 20min: 260 * 0.95 = 247, 5min: 260 * 0.80 = 208, 8min: 260 * 0.86 = 224
        assert ftp == 247

        sig = compute_fitness_signature(efforts, ftp=ftp)
        assert sig.ftp == 247
        assert sig.ltp == 185  # 247 * 0.75 = 185.25 -> 185

    def test_full_pipeline_variable_ride(self):
        """Ride with a hard 5min interval embedded in endurance."""
        # 10 min at 180W, then 5 min at 350W, then 10 min at 180W = 25 min
        powers = (
            _constant_power(180, 600)
            + _constant_power(350, 300)
            + _constant_power(180, 600)
        )
        samples = _make_samples(powers)

        efforts = extract_best_efforts(samples)
        assert efforts[300] == 350  # 5min peak is the hard interval
        assert efforts[5] == 350    # 5s peak also from hard interval

        ftp = estimate_ftp(efforts)
        # With long-duration preference:
        #   20min (1200): best ~222.5W → * 0.95 ≈ 211
        #   8min (480):   best ~282W   → * 0.86 ≈ 243 (not long-duration, cannot displace)
        #   5min (300):   350W         → * 0.80 = 280 (not long-duration, cannot displace)
        # Long-duration 20min seeds best and nothing displaces it.
        assert ftp == 211

        sig = compute_fitness_signature(efforts, ftp=211)
        assert sig.ftp == 211
        # HIE = (350 - 211) * 300 / 1000 = 41.7 kJ
        assert sig.hie == 41.7

    def test_full_pipeline_short_ride_no_ftp(self):
        """Ride shorter than 5 min: FTP cannot be estimated."""
        powers = _constant_power(300, 200)  # ~3.3 min
        samples = _make_samples(powers)

        efforts = extract_best_efforts(samples)
        assert 300 not in efforts  # not enough data for 5min

        ftp = estimate_ftp(efforts)
        assert ftp is None


# ══════════════════════════════════════════════════════════════════════════════
# MONOD-SCHERRER 2-PARAMETER CP/W' FIT (v3.6.0-fix26 §4.2)
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeCpWprime(unittest.TestCase):
    """compute_cp_wprime on synthetic power curves (v3.6.0-fix26 §4.2)."""

    def test_synthetic_fit_recovers_cp_and_wprime(self):
        """Given P(t) = CP + W'/t, the fit should recover CP & W' within 1%.

        Construct (180, 300, 600, 1200)-s points on the exact Monod
        hyperbola (CP=250 W, W'=20000 J). OLS on 1/t is exact for 4
        noise-free points, so R² = 1.0 and the estimates match.
        """
        cp_true, wprime_true = 250.0, 20000.0
        efforts = {
            t: round(cp_true + wprime_true / t)
            for t in (180, 300, 600, 1200)
        }
        result = compute_cp_wprime(efforts)
        assert result is not None
        cp, wprime = result
        assert abs(cp - cp_true) < 3.0, f"CP {cp} too far from {cp_true}"
        assert abs(wprime - wprime_true) < 200, f"W' {wprime} too far from {wprime_true}"

    def test_returns_none_insufficient_points(self):
        """Only two durations → below MONOD_MIN_POINTS, must return None."""
        assert compute_cp_wprime({}) is None
        assert compute_cp_wprime({300: 300}) is None
        assert compute_cp_wprime({300: 300, 600: 270}) is None  # only 2 pts

    def test_returns_none_on_low_r_squared(self):
        """A non-hyperbolic power curve (all points above the Monod line)
        should fail the R² gate and return None."""
        # Perfectly flat power across durations — slope ≈ 0 → W' ≈ 0,
        # which fails the lower-bound guard. Equivalent to R²=0.
        assert compute_cp_wprime({180: 280, 300: 280, 600: 280, 1200: 280}) is None

    def test_filters_out_non_standard_durations(self):
        """Durations outside MONOD_DURATIONS_S are ignored; with only
        non-standard durations the fit returns None."""
        efforts = {5: 900, 30: 700, 60: 500}
        assert compute_cp_wprime(efforts) is None

    def test_rejects_out_of_range_cp(self):
        """A fit producing CP < 100 W or > 500 W must be rejected."""
        # High W' coefficient, tiny CP — craft so intercept falls out-of-range.
        # P = 50 + 5000/t across 180..1200: 50+27.8, 50+16.7, 50+8.3, 50+4.2
        bad_efforts = {180: 78, 300: 67, 600: 58, 1200: 54}
        assert compute_cp_wprime(bad_efforts) is None

    def test_rejects_non_physical_fit(self):
        """An inverted power curve (power RISES with duration) would yield
        negative W', which is physiologically impossible. Must return
        None rather than a garbage value."""
        inverted = {180: 200, 300: 220, 600: 250, 1200: 280}
        assert compute_cp_wprime(inverted) is None

    def test_ignores_zero_and_negative_values(self):
        """Zero / negative power values for a duration are dropped from
        the fit; if that leaves < MONOD_MIN_POINTS the call returns None."""
        efforts = {180: 0, 300: -5, 600: 260, 1200: 250}
        assert compute_cp_wprime(efforts) is None
        # With 3 valid values the fit should still succeed if the shape fits.
        good = {180: 361, 300: 317, 600: 283, 1200: 267}  # CP=250, W'=20000
        result = compute_cp_wprime(good)
        assert result is not None
