"""v1.0.7 IMPL-DFA-ALPHA1 — DFA α1 from raw FIT tests.

Synthetic-RR golden tests + FIT round-trip integration tests.

Maths goldens (Rogers 2021 / Peng 1995 expectations):
  * White noise (small Gaussian) → α1 ≈ 0.5 (limit case for constant RR).
  * 1/f (pink) noise             → α1 ≈ 1.0.
  * Cumulative random walk
    (Brownian)                   → α1 ≈ 1.5.

FIT integration:
  * parse_rr_intervals() round-trips a synthesised FIT with N RR values.
  * compute_dfa_alpha1_for_fit() returns ``status='no_rr_data'`` for a
    FIT without HrvMessage records.
  * compute_dfa_alpha1_for_fit() returns ``status='computed'`` and an
    α1 in the physiological range for a synthetic-RR fixture.
"""
from __future__ import annotations

import math
import random
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT))

from analytics import (  # noqa: E402
    DFA_HRVT1_ALPHA,
    DFA_HRVT2_ALPHA,
    DFA_SANITY_MAX,
    DFA_SANITY_MIN,
    _dfa_alpha1_window,
    _filter_rr_artifacts,
    _interp_load_at_alpha,
    compute_dfa_alpha1,
    compute_dfa_alpha1_for_fit,
    compute_dfa_threshold_analysis,
)
from fit_activity import parse_rr_intervals  # noqa: E402


# ── helpers ─────────────────────────────────────────────────────────────────

def _seeded_white_noise(n: int, mean: float = 1.0, sd: float = 0.05,
                        seed: int = 42) -> list[float]:
    """Stationary white-noise RR series — DFA α1 expectation ≈ 0.5."""
    rng = random.Random(seed)
    return [max(0.4, mean + rng.gauss(0, sd)) for _ in range(n)]


def _seeded_brownian(n: int, mean: float = 1.0, sd: float = 0.01,
                     seed: int = 42) -> list[float]:
    """Cumulative random walk RR series — DFA α1 expectation ≈ 1.5.

    Each beat = previous beat + small Gaussian step. Floor at 0.4 s to keep
    the walk physiologically plausible.
    """
    rng = random.Random(seed)
    out = []
    v = mean
    for _ in range(n):
        v += rng.gauss(0, sd)
        out.append(max(0.4, v))
    return out


def _seeded_pink_fft(n: int, mean: float = 1.0, sd: float = 0.05,
                     seed: int = 42) -> list[float]:
    """1/f (pink) noise via inverse-DFT shaping — DFA α1 expectation ≈ 1.0.

    Naive O(N²) IDFT — fine for n ~ 2000-3000 in the test suite.
    """
    rng = random.Random(seed)
    nh = n // 2 + 1
    real = [0.0] * nh
    imag = [0.0] * nh
    for k in range(1, nh):
        amp = 1.0 / math.sqrt(k)
        phase = rng.uniform(0, 2 * math.pi)
        real[k] = amp * math.cos(phase)
        imag[k] = amp * math.sin(phase)
    raw = []
    for t in range(n):
        s = 0.0
        for k in range(nh):
            angle = 2 * math.pi * k * t / n
            s += real[k] * math.cos(angle) - imag[k] * math.sin(angle)
        raw.append(s / n)
    raw_mean = sum(raw) / n
    raw_sd = math.sqrt(sum((x - raw_mean) ** 2 for x in raw) / n) or 1.0
    return [max(0.4, mean + sd * (x - raw_mean) / raw_sd) for x in raw]


def _build_fit_with_rr(rr_seconds: list[float]) -> Path:
    """Synthesise a minimal FIT activity carrying N HrvMessage records.

    Each record packs 5 RR-values (zero-padded if fewer remain). Returns the
    path to a temp file the caller is expected to clean up.
    """
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.hrv_message import HrvMessage
    from fit_tool.profile.profile_type import FileType, Manufacturer

    b = FitFileBuilder()
    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.DEVELOPMENT.value
    fid.product = 0
    fid.serial_number = 4242
    b.add(fid)
    i = 0
    while i < len(rr_seconds):
        chunk = rr_seconds[i:i + 5]
        # Pad to 5 with zeros per the FIT spec.
        while len(chunk) < 5:
            chunk.append(0.0)
        m = HrvMessage()
        m.time = chunk
        b.add(m)
        i += 5

    ff = b.build()
    tf = tempfile.NamedTemporaryFile(suffix=".fit", delete=False)
    tf.close()
    ff.to_file(tf.name)
    return Path(tf.name)


def _build_fit_no_hrv() -> Path:
    """Synthesise a minimal FIT with NO HrvMessage records (FileId only)."""
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.profile_type import FileType, Manufacturer

    b = FitFileBuilder()
    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.DEVELOPMENT.value
    fid.product = 0
    fid.serial_number = 4243
    b.add(fid)
    ff = b.build()
    tf = tempfile.NamedTemporaryFile(suffix=".fit", delete=False)
    tf.close()
    ff.to_file(tf.name)
    return Path(tf.name)


# ── synthetic-RR maths goldens ──────────────────────────────────────────────

class TestDFASyntheticGoldens(unittest.TestCase):
    """The three reference signals from the literature."""

    def test_white_noise_alpha_low(self):
        """White noise (low-σ Gaussian RR) → α1 ≈ 0.5 ± 0.2.

        Pure constant RR is the degenerate limit (F(n) = 0 ⇒ undefined α1);
        the canonical Rogers 2021 case is small-amplitude white noise.
        """
        white = _seeded_white_noise(2000, mean=1.0, sd=0.02, seed=1)
        out = compute_dfa_alpha1(white)
        self.assertIsNotNone(out["avg"], "white-noise fit should produce a value")
        self.assertGreater(out["n_windows"], 5)
        self.assertLess(out["avg"], 0.70,
                        f"white-noise α1 should be < 0.70, got {out['avg']}")
        self.assertGreater(out["avg"], 0.30,
                           f"white-noise α1 should be > 0.30, got {out['avg']}")

    def test_pink_noise_alpha_about_one(self):
        """1/f (pink) noise → α1 ≈ 1.0 ± 0.15."""
        rr = _seeded_pink_fft(2500, sd=0.05, seed=42)
        out = compute_dfa_alpha1(rr)
        self.assertIsNotNone(out["avg"])
        self.assertGreater(out["n_windows"], 5)
        self.assertGreater(out["avg"], 0.85,
                           f"pink-noise α1 should be > 0.85, got {out['avg']}")
        self.assertLess(out["avg"], 1.20,
                        f"pink-noise α1 should be < 1.20, got {out['avg']}")

    def test_brownian_alpha_about_one_point_five(self):
        """Cumulative random walk (Brownian) RR → α1 ≈ 1.5 ± 0.2."""
        rr = _seeded_brownian(2000, sd=0.005, seed=3)
        out = compute_dfa_alpha1(rr)
        self.assertIsNotNone(out["avg"])
        self.assertGreater(out["n_windows"], 5)
        self.assertGreater(out["avg"], 1.30,
                           f"Brownian α1 should be > 1.30, got {out['avg']}")
        # Sanity-gate caps at 1.60; just require we're well above the white
        # / pink band.
        self.assertLessEqual(out["avg"], DFA_SANITY_MAX)


# ── parse_rr_intervals + compute_dfa_alpha1_for_fit integration ─────────────

class TestParseRRIntervals(unittest.TestCase):
    """End-to-end: synthesised FIT → RR-list → DFA α1."""

    def test_round_trip_100_rr_values(self):
        """parse_rr_intervals on a FIT with 100 RR values returns 100 floats
        in chronological order, within FIT-spec quantisation tolerance.

        FIT encodes ``hrv.time`` at 1/1024-s precision (the field's scale),
        so byte-exact equality after re-decode is unrealistic. Tolerance ≤
        2 ms covers the encoding-induced rounding.
        """
        rng = random.Random(7)
        rr = [round(0.6 + rng.uniform(0, 0.4), 4) for _ in range(100)]
        fit_path = _build_fit_with_rr(list(rr))
        try:
            parsed = parse_rr_intervals(fit_path)
            self.assertEqual(len(parsed), 100,
                             f"expected 100 RR values, got {len(parsed)}")
            for x in parsed:
                self.assertIsInstance(x, float)
                self.assertGreater(x, 0.0)
            # Each value within FIT quantisation of the original AND
            # chronological order preserved.
            for original, decoded in zip(rr, parsed):
                self.assertAlmostEqual(decoded, original, places=2)
        finally:
            fit_path.unlink(missing_ok=True)

    def test_no_hrv_messages_returns_empty(self):
        """parse_rr_intervals on a FIT without HrvMessage returns []."""
        fit_path = _build_fit_no_hrv()
        try:
            self.assertEqual(parse_rr_intervals(fit_path), [])
        finally:
            fit_path.unlink(missing_ok=True)


class TestComputeDFAForFit(unittest.TestCase):
    """status-field semantics check."""

    def test_no_rr_status(self):
        """A FIT with no HrvMessage returns status='no_rr_data', avg=None."""
        fit_path = _build_fit_no_hrv()
        try:
            out = compute_dfa_alpha1_for_fit(fit_path)
            self.assertIsInstance(out, dict)
            self.assertEqual(out["dfa_alpha1_status"], "no_rr_data")
            self.assertIsNone(out["dfa_alpha1_avg"])
            self.assertEqual(out["rr_intervals_count"], 0)
            self.assertEqual(out["dfa_alpha1_series"], [])
        finally:
            fit_path.unlink(missing_ok=True)

    def test_synthetic_rr_status_computed_and_in_range(self):
        """A FIT with ~2500 synthetic RR values returns status='computed' and
        α1 ∈ [0.4, 1.5]. Pink-noise fixture lands ~1.0; the gate just verifies
        we're inside the physiological band.
        """
        rr = _seeded_pink_fft(2500, sd=0.05, seed=11)
        fit_path = _build_fit_with_rr(list(rr))
        try:
            out = compute_dfa_alpha1_for_fit(fit_path)
            self.assertIsInstance(out, dict)
            self.assertEqual(out["dfa_alpha1_status"], "computed")
            self.assertIsNotNone(out["dfa_alpha1_avg"])
            self.assertGreater(out["dfa_alpha1_avg"], 0.4)
            self.assertLess(out["dfa_alpha1_avg"], 1.5)
            self.assertGreater(out["rr_intervals_count"], 1000)
            self.assertGreater(len(out["dfa_alpha1_series"]), 5)
            # Each series entry has the documented shape.
            entry = out["dfa_alpha1_series"][0]
            self.assertIn("min", entry)
            self.assertIn("alpha1", entry)
            self.assertGreaterEqual(entry["alpha1"], DFA_SANITY_MIN)
            self.assertLessEqual(entry["alpha1"], DFA_SANITY_MAX)
        finally:
            fit_path.unlink(missing_ok=True)


class TestRRArtifactFilter(unittest.TestCase):
    """v1.8.14 — Malik RR-artifact rejection.

    DFA α1 is acutely artifact-sensitive: ~1% ectopic/missed beats drag α1
    toward the 0.5 white-noise floor AND break the per-window R² gate so most
    windows get rejected. These tests guard the fix that closes that bug
    (validated on a real ride: 0.573 → 1.16 after filtering 1.3% of beats).
    """

    def test_filter_drops_spikes_keeps_clean(self):
        # Clean ~1 Hz series (1000 ms RR) with two gross spikes injected.
        rr = [1.0] * 200
        rr[50] = 0.4   # +missed-beat halving (>20% from 1.0)
        rr[120] = 1.8  # +inserted-beat (>20% from 1.0)
        filtered, dropped = _filter_rr_artifacts(rr, rel_thresh=0.20)
        self.assertEqual(dropped, 2)
        self.assertEqual(len(filtered), 198)
        # No retained beat deviates >20% from its predecessor.
        for i in range(1, len(filtered)):
            self.assertLessEqual(
                abs(filtered[i] - filtered[i - 1]) / filtered[i - 1], 0.20)

    def test_filter_empty_and_singleton(self):
        self.assertEqual(_filter_rr_artifacts([]), ([], 0))
        self.assertEqual(_filter_rr_artifacts([0.85]), ([0.85], 0))

    def test_filter_no_cascade_from_single_spike(self):
        # A lone spike must drop ONLY itself — comparison is against the last
        # ACCEPTED beat, not the raw previous, so the series resumes cleanly.
        rr = [0.9] * 10 + [5.0] + [0.9] * 10
        filtered, dropped = _filter_rr_artifacts(rr, rel_thresh=0.20)
        self.assertEqual(dropped, 1)
        self.assertEqual(len(filtered), 20)

    def test_artifacts_crater_alpha_then_filter_recovers(self):
        # Build a clean pink-noise RR series (α1 ≈ 1.0), inject 1.5% spikes,
        # and prove the END-TO-END compute (which now filters internally)
        # stays in the aerobic band instead of collapsing toward 0.5.
        rr = list(_seeded_pink_fft(4000, sd=0.04, seed=7))
        clean = compute_dfa_alpha1(rr)
        self.assertIsNotNone(clean["avg"])
        # Inject artifacts: every ~66th beat becomes a gross ectopic.
        corrupted = list(rr)
        for i in range(30, len(corrupted), 66):
            corrupted[i] = corrupted[i] * (1.6 if (i // 66) % 2 else 0.45)
        filtered = compute_dfa_alpha1(corrupted)
        self.assertIsNotNone(filtered["avg"],
            "internal artifact filter should let a corrupted series still compute")
        # Filtered result must stay near the clean α1 (within 0.20), NOT
        # collapse toward the 0.5 white-noise floor.
        self.assertAlmostEqual(filtered["avg"], clean["avg"], delta=0.20)
        self.assertGreater(filtered["avg"], 0.7,
            "filtered α1 must not collapse toward the white-noise floor")


class TestThresholdDetection(unittest.TestCase):
    """v1.8.14 — HRVT1/HRVT2 interpolation + intensity distribution."""

    def test_interp_load_at_alpha_linear(self):
        # Synthetic windows where α1 falls linearly 1.0 → 0.4 as power rises
        # 150 → 330 W. α1 = 0.75 should land at power = 225 W.
        windows = []
        for i in range(20):
            power = 150 + i * (180 / 19)          # 150..330
            alpha = 1.0 - (power - 150) / 180 * 0.6  # 1.0..0.4
            windows.append({"power": power, "hr": None, "alpha1": alpha})
        fit = _interp_load_at_alpha(windows, 0.75, "power")
        self.assertIsNotNone(fit)
        self.assertAlmostEqual(fit["load"], 225.0, delta=5.0)
        self.assertGreater(fit["r2"], 0.99)

    def test_interp_returns_none_when_target_not_bracketed(self):
        # All windows easy (α1 0.9–1.1): 0.5 is never reached → no HRVT2.
        windows = [{"power": 100 + i, "hr": None, "alpha1": 0.9 + 0.01 * (i % 3)}
                   for i in range(15)]
        self.assertIsNone(_interp_load_at_alpha(windows, 0.50, "power"))

    def test_threshold_analysis_zone_minutes_and_detection(self):
        # Build a ramp: 40 one-second-ish "slots" won't do; synthesise a
        # per-second hrv stream where RR shortens (HR rises) over 30 min so
        # α1 sweeps 1.0 → 0.45, with aligned power 150 → 320 W.
        import random
        rng = random.Random(3)
        dur_s = 1800
        hrv, hr_s, pw_s = [], [], []
        for sec in range(dur_s):
            frac = sec / dur_s
            # mean RR 1100 ms (easy) → 480 ms (hard); ~more beats/sec as HR rises
            mean_rr = 1100 - frac * 620
            # noise scales UP with intensity → α1 falls (less correlated)
            sd = 8 + frac * 55
            beats = max(1, int(round(1000.0 / mean_rr)))
            slot = [max(250, int(rng.gauss(mean_rr, sd))) for _ in range(beats)]
            hrv.append(slot)
            hr_s.append(60000.0 / mean_rr)
            pw_s.append(150 + frac * 170)
        res = compute_dfa_threshold_analysis(hrv, hr_s, pw_s)
        self.assertGreaterEqual(res["n_windows"], 8)
        z = res["zone_minutes"]
        # Ramp should spend time in at least the easy + hard zones.
        self.assertGreater(z["z1"], 0)
        self.assertGreater(z["z3"], 0)
        # Zone minutes = VALID windows × step; rejected windows (poor R² fit
        # at high intensity) don't count, so the sum is ≤ ride length, and
        # equals n_windows × 0.5 min exactly.
        total = z["z1"] + z["z2"] + z["z3"]
        self.assertLessEqual(total, dur_s / 60.0 + 0.1)
        self.assertAlmostEqual(total, res["n_windows"] * 0.5, delta=0.6)
        # HRVT1 must resolve on a clean ramp; power should fall in the ramp range.
        self.assertIsNotNone(res["hrvt1"])
        if res["hrvt1"]["power"] is not None:
            self.assertTrue(150 <= res["hrvt1"]["power"] <= 320)

    def test_threshold_analysis_empty_stream(self):
        res = compute_dfa_threshold_analysis([], [], [])
        self.assertEqual(res["n_windows"], 0)
        self.assertIsNone(res["hrvt1"])
        self.assertIsNone(res["hrvt2"])


class TestComputeDFAFromHRVStream(unittest.TestCase):
    """v1.8.10 — lazy DFA compute from ICU's hrv-stream channel.

    The stream is a per-second list where each non-null slot is a list
    of RR-interval ints in milliseconds — same physical content as the
    FIT HrvMessage records, just packaged differently. This test class
    verifies the lazy path produces the same status semantics as
    compute_dfa_alpha1_for_fit.
    """

    def test_none_input_returns_none(self):
        from analytics import compute_dfa_alpha1_from_hrv_stream
        self.assertIsNone(compute_dfa_alpha1_from_hrv_stream(None))
        self.assertIsNone(compute_dfa_alpha1_from_hrv_stream("not a list"))

    def test_empty_stream_returns_no_rr_data(self):
        from analytics import compute_dfa_alpha1_from_hrv_stream
        out = compute_dfa_alpha1_from_hrv_stream([None] * 60)
        self.assertIsInstance(out, dict)
        self.assertEqual(out["dfa_alpha1_status"], "no_rr_data")
        self.assertEqual(out["rr_intervals_count"], 0)

    def test_padding_and_sentinel_filtered(self):
        from analytics import compute_dfa_alpha1_from_hrv_stream
        # 0 ms (padding) and 65535 (sentinel) must be filtered.
        stream = [[0, 800], [65535], [820, 0]]
        out = compute_dfa_alpha1_from_hrv_stream(stream)
        self.assertIsInstance(out, dict)
        # 2 valid RR (800, 820) — too few for DFA → no_rr_data status,
        # but rr_intervals_count must reflect the filter result.
        self.assertEqual(out["rr_intervals_count"], 2)

    def test_synthetic_pink_rr_status_computed_and_matches_fit_path(self):
        """Same RR sequence delivered as FIT vs as hrv-stream must
        produce α1 within 0.05 of each other (rounding + ~1-2 extra
        beats captured by the stream's per-second boundaries).
        """
        from analytics import (
            compute_dfa_alpha1_for_fit,
            compute_dfa_alpha1_from_hrv_stream,
        )
        # Re-use the same pink-noise fixture as the FIT test.
        rr = list(_seeded_pink_fft(2500, sd=0.05, seed=11))
        fit_path = _build_fit_with_rr(rr)
        try:
            out_fit = compute_dfa_alpha1_for_fit(fit_path)
            # Pack the SAME rr sequence into a per-second hrv-stream.
            # Each second can hold up to 5 RR ints; we approximate by
            # chunking 1-2 per "second" based on a 1 Hz time axis.
            ms = [int(round(s * 1000)) for s in rr]
            stream: list = []
            i = 0
            while i < len(ms):
                # 1-2 beats per second alternation — mimics real-life
                # variation without depending on a clock signal.
                chunk = ms[i:i + (1 if i % 3 else 2)]
                stream.append(chunk)
                i += len(chunk)
            out_stream = compute_dfa_alpha1_from_hrv_stream(stream)
            self.assertEqual(out_fit["dfa_alpha1_status"], "computed")
            self.assertEqual(out_stream["dfa_alpha1_status"], "computed")
            self.assertAlmostEqual(
                out_fit["dfa_alpha1_avg"],
                out_stream["dfa_alpha1_avg"],
                delta=0.05,
                msg=("fit vs stream α1 must agree within 0.05; "
                     f"fit={out_fit['dfa_alpha1_avg']} "
                     f"stream={out_stream['dfa_alpha1_avg']}"),
            )
        finally:
            fit_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
