"""Tests for the aperiodic shape primitives in ``route_archetypes``.

Agent P's rewrite replaces sinusoidal shape functions with fBm +
Poisson-placed features + Markov-chain grade states. These tests enforce
that the resulting grade series look like REAL road elevation:

- No sine/cos calls in the primitive file
- Flat sections are actually flat (SD < 0.8, |climb rate| < 5 m/km)
- Rolling sections are aperiodic (median max_autocorr < 0.5 over 50 seeds)
- fBm preserves sane variance
- Poisson placement has exponential-gap signature
- Asymmetric hill is asymmetric
- Markov climb visits ≥ 3 distinct grade levels
- Most Markov climbs have at least one relief/flat segment
"""

from __future__ import annotations

import math
import re
import statistics
from pathlib import Path

import pytest

from route_archetypes import (
    SEG_KM,
    Section,
    _asymmetric_hill,
    _fbm_1d,
    _layered_rolling,
    _markov_climb,
    _max_autocorr,
    _poisson_positions,
    _shape_section,
)


ROUTE_FILE = Path(__file__).resolve().parent.parent / "src" / "route_archetypes.py"


# ─────────────────────────────────────────────────────────────────────────────
# 1. No sine/cos in the primitives module (code — comments/docstrings OK)
# ─────────────────────────────────────────────────────────────────────────────

def test_no_sine_cos_in_shape_file():
    """route_archetypes.py must not call math.sin / math.cos in any code.

    Occurrences inside comments (lines starting with #) and inside triple-
    quoted docstrings are allowed; the regex scans only *code* lines.
    """
    src = ROUTE_FILE.read_text(encoding="utf-8")

    # Strip docstrings (triple-quoted strings)
    no_docstrings = re.sub(r'"""[\s\S]*?"""', "", src)
    no_docstrings = re.sub(r"'''[\s\S]*?'''", "", no_docstrings)
    # Strip single-line comments
    code_only = re.sub(r"#[^\n]*", "", no_docstrings)

    # Now look for sine/cosine calls in the code
    assert re.search(r"\bmath\.sin\b", code_only) is None, (
        "math.sin found in code — must be replaced with fBm"
    )
    assert re.search(r"\bmath\.cos\b", code_only) is None, (
        "math.cos found in code — must be replaced with fBm"
    )
    # Also catch bare sin(/cos( if somebody did `from math import sin`
    assert re.search(r"(?<![A-Za-z_])sin\(", code_only) is None, (
        "bare sin( call found in code"
    )
    assert re.search(r"(?<![A-Za-z_])cos\(", code_only) is None, (
        "bare cos( call found in code"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Flat sections actually look flat
# ─────────────────────────────────────────────────────────────────────────────

def test_flat_section_actually_flat():
    """A 10 km flat section must have SD < 0.8 and near-zero climb rate."""
    length_km = 10.0
    n = round(length_km / SEG_KM)
    sds: list[float] = []
    climb_rates: list[float] = []
    for trial in range(25):
        sec = Section(kind="flat", length_km=length_km, params={"grade": 0.0})
        grades = _shape_section(sec, n, seed=trial * 101 + 7)
        sds.append(statistics.stdev(grades))
        climb_m = sum(SEG_KM * 1000 * g / 100 for g in grades if g > 0)
        descent_m = sum(SEG_KM * 1000 * -g / 100 for g in grades if g < 0)
        # Net climb rate magnitude (m per km)
        climb_rates.append(abs(climb_m - descent_m) / length_km)
    median_sd = statistics.median(sds)
    median_rate = statistics.median(climb_rates)
    assert median_sd < 0.8, f"flat SD too high: {median_sd:.2f}"
    assert median_rate < 5.0, f"flat climb rate too high: {median_rate:.2f} m/km"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Rolling sections are aperiodic
# ─────────────────────────────────────────────────────────────────────────────

def test_rolling_section_aperiodic():
    """50 random seeds of a 10 km rolling section — median max_autocorr < 0.5."""
    length_km = 10.0
    n = round(length_km / SEG_KM)
    peaks = []
    for trial in range(50):
        sec = Section(kind="rolling", length_km=length_km,
                      params={"baseline": 0.0, "amp": 2.5, "period_km": 1.2})
        grades = _shape_section(sec, n, seed=trial * 98765 + 42)
        peaks.append(_max_autocorr(grades))
    median = statistics.median(peaks)
    assert median < 0.5, (
        f"rolling section has sinusoidal-like autocorr: median={median:.3f}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. fBm preserves variance
# ─────────────────────────────────────────────────────────────────────────────

def test_fbm_preserves_variance():
    """Sample fBm over a long interval — std-dev must sit in a sane range."""
    samples = []
    for i in range(2000):
        samples.append(_fbm_1d(i * 0.37, seed=123, octaves=5))
    sd = statistics.stdev(samples)
    # Normalised fBm (/ max_amp) should have SD roughly in [0.1, 0.5]
    assert 0.05 < sd < 0.7, f"fBm std-dev out of expected range: {sd:.3f}"
    # Mean should be close to 0
    assert abs(statistics.mean(samples)) < 0.3, \
        f"fBm mean drifted: {statistics.mean(samples):.3f}"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Poisson-placed positions have exponential-gap signature
# ─────────────────────────────────────────────────────────────────────────────

def test_poisson_spacing_has_variance():
    """Exponential inter-arrival gaps have coefficient-of-variation = 1.

    Aggregate gaps from 20 separate Poisson placements; std/mean must
    exceed 0.5 (well above any grid-like placement's ~0).
    """
    all_gaps: list[float] = []
    for trial in range(20):
        positions = _poisson_positions(length_km=100.0,
                                       mean_spacing_km=1.5,
                                       seed=trial * 13 + 7)
        # Add gap from 0 to first, between consecutive, and to end
        if not positions:
            continue
        all_gaps.append(positions[0])
        for a, b in zip(positions, positions[1:]):
            all_gaps.append(b - a)
    assert len(all_gaps) > 50, "not enough gaps to measure variance"
    mean = statistics.mean(all_gaps)
    sd = statistics.stdev(all_gaps)
    cv = sd / mean
    assert cv > 0.5, (
        f"Poisson gap CV too low ({cv:.2f}) — placement is too uniform"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Asymmetric hill is asymmetric
# ─────────────────────────────────────────────────────────────────────────────

def test_asymmetric_hill_asymmetric():
    """rise_sigma=0.1, fall_sigma=0.3 ⇒ ascent side narrower than descent."""
    peak = 10.0
    center = 0.0
    # Find the ascending half-width (where value drops to peak/2 on left)
    left = 0.0
    for dx_mm in range(1, 1000):
        x = -dx_mm * 0.001  # go left
        if _asymmetric_hill(x, center, 0.1, 0.3, peak) < peak * 0.5:
            left = abs(x)
            break
    right = 0.0
    for dx_mm in range(1, 1000):
        x = dx_mm * 0.001  # go right
        if _asymmetric_hill(x, center, 0.1, 0.3, peak) < peak * 0.5:
            right = x
            break
    # With rise_sigma < fall_sigma, descending side should be wider
    assert right > left, (
        f"expected right side wider: left={left:.3f}, right={right:.3f}"
    )
    assert right > left * 1.5, (
        f"right not sufficiently wider: left={left:.3f}, right={right:.3f}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Markov climb visits at least 3 distinct grade levels
# ─────────────────────────────────────────────────────────────────────────────

def test_markov_climb_diverse_states():
    """A long Markov climb should exhibit ≥ 3 distinct grade levels.

    Distinct levels are measured by rounded integer grades with ≥ 3
    samples each. A sinusoidal or constant-mean profile would cluster
    into 1–2 buckets; a Markov state-switching profile spans 5+.
    """
    for trial in range(20):
        grades = _markov_climb(length_km=10.0, seed=trial * 73 + 1,
                               start_state="tempo")
        # Count integer buckets that have at least 3 samples
        from collections import Counter
        c = Counter(round(g) for g in grades)
        populated = sum(1 for _, v in c.items() if v >= 3)
        assert populated >= 3, (
            f"trial {trial}: only {populated} rounded grade levels "
            f"with ≥ 3 samples (counts={dict(c)})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 8. Markov climbs usually contain a relief/low-grade segment
# ─────────────────────────────────────────────────────────────────────────────

def test_climb_has_plateau_states():
    """>= 70% of runs have at least one segment with grade < 4% (relief)."""
    has_relief = 0
    n_trials = 30
    for trial in range(n_trials):
        grades = _markov_climb(length_km=8.0, seed=trial * 41 + 9,
                               start_state="hard")
        if any(g < 4.0 for g in grades):
            has_relief += 1
    frac = has_relief / n_trials
    assert frac >= 0.70, (
        f"Only {has_relief}/{n_trials} climbs had relief — "
        "Markov chain not visiting relief/flat/tempo enough"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 9. Section shape outputs are the right length
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", [
    "flat", "false_flat_up", "false_flat_down", "short_hill", "kicker_up",
    "gradual_climb", "sustained_climb", "steep_wall", "rolling", "descent",
    "plateau",
])
def test_shape_section_returns_n_segments(kind):
    """Every shape function must emit exactly ``n`` grade values."""
    n = 120
    sec = Section(kind=kind, length_km=n * SEG_KM,
                  params={"grade": 0.0, "peak": 7.0, "avg": 7.0,
                          "amp": 3.0, "roll_amp": 2.0, "baseline": 0.0,
                          "period_km": 1.5})
    out = _shape_section(sec, n, seed=12345)
    assert len(out) == n, f"{kind} returned {len(out)} values (expected {n})"


# ─────────────────────────────────────────────────────────────────────────────
# 10. Layered rolling produces aperiodic output across many seeds
# ─────────────────────────────────────────────────────────────────────────────

def test_layered_rolling_aperiodic():
    """_layered_rolling — median autocorr below 0.5 over 30 seeds."""
    peaks = []
    for trial in range(30):
        grades = _layered_rolling(
            length_km=10.0, seed=trial * 31 + 5,
            baseline_grade=0.0, amp_macro=1.5, amp_micro=0.8,
            feature_mean_spacing=1.0, feature_peak_range=(3.0, 8.0),
        )
        peaks.append(_max_autocorr(grades))
    assert statistics.median(peaks) < 0.5


# ─────────────────────────────────────────────────────────────────────────────
# 11. fBm output is different for different seeds (not constant)
# ─────────────────────────────────────────────────────────────────────────────

def test_fbm_seed_varies_output():
    """fBm with different seeds produces meaningfully different series."""
    a = [_fbm_1d(i * 0.3, seed=1) for i in range(300)]
    b = [_fbm_1d(i * 0.3, seed=2) for i in range(300)]
    # MSE between the two series should be non-trivial
    mse = sum((ai - bi) ** 2 for ai, bi in zip(a, b)) / len(a)
    assert mse > 0.01, f"fBm not seed-dependent enough: MSE={mse}"
