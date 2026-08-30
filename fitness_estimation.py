"""FTP auto-estimation and Xert-style fitness signature computation.

Implements three features for post-ride analysis:
  1. FTP estimation from best-effort power data (FTP estimation from power duration curve)
  2. Fitness signature (FTP, LTP, HIE, Peak Power) from ride history
  3. Monod-Scherrer 2-parameter CP/W' fit from best-efforts (v3.6.0-fix26
     §4.2: fallback when Intervals.icu wPrime is unavailable).

Data flow:
    RideSample[] -> extract_best_efforts() -> best_efforts dict
    best_efforts -> estimate_ftp() -> FTP estimate
    best_efforts + FTP -> compute_fitness_signature() -> FitnessSignature
    best_efforts -> compute_cp_wprime() -> (CP, W') tuple | None
"""

from __future__ import annotations

import logging
import math
import re
from collections import deque
from dataclasses import dataclass
from typing import Optional

from training_live import RideSample

log = logging.getLogger(__name__)
# v3.6.0-fix30-logs-ext: named category logger for Monod + post-hoc
# decoupling emissions. Uses the `domestique.power` category so a ride-log
# grep for `EVENT=monod_fit|EVENT=aerobic_decoupling_computed` locates
# every fit attempt in one place. No log_config import here to avoid
# circular-import risk during test bootstrap.
log_power = logging.getLogger("domestique.power")


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

# Standard durations (seconds) for best-effort extraction.
# v1.3.0 widened from 8 to 12 tiers (added 1, 15, 120, 600 s) so the Power
# Curve module can render fast-twitch sprints, neuromuscular bands, and
# 10-minute climbs. Verified safe per /tmp/audit_v130_power_curve.md §3:
#   estimate_ftp filters by MIN_FTP_EFFORT_DURATION (300 s) — extra short
#   tiers are ignored. compute_cp_wprime filters to MONOD_DURATIONS_S
#   (180/300/600/1200) — adding 600 doesn't change behaviour because 600
#   was already in MONOD_DURATIONS_S. compute_fitness_signature reads
#   only 5 and 300, both still present.
STANDARD_DURATIONS = [1, 5, 15, 30, 60, 120, 300, 480, 600, 1200, 1800, 3600]

# FTP scaling factors: {duration_seconds: multiplier}
# FTP = best_watts_for_duration * multiplier
# Source: standard coaching heuristics (Coggan & Allen).
#
# NOTE: the 20-minute factor (0.95) is the classic Allen/Coggan heuristic but is
# known to vary with athlete level. Valenzuela et al. 2023 (Int J Sports Physiol
# Perform 18:559) report individual 20-min-to-FTP ratios between ~0.88 (trained
# amateurs who sustain a flatter power-duration curve) and ~0.96 (elite
# time-trial specialists with steeper drop-off). Users with individual data
# (e.g. a recent 60-minute TT or lab-measured FTP) should self-calibrate this
# factor instead of relying on the global default.
FTP_SCALING_FACTORS: dict[int, float] = {
    300:  0.80,   # 5min — VO2max limited, rough estimate
    480:  0.86,   # 8min
    1200: 0.95,   # 20min — classic FTP test (Valenzuela 2023: 0.88–0.96 range)
    1800: 0.97,   # 30min
    3600: 1.00,   # 60min — gold standard
}

# Minimum effort duration (seconds) required to estimate FTP
MIN_FTP_EFFORT_DURATION = 300  # 5 minutes


# ══════════════════════════════════════════════════════════════════════════════
# DATACLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class FitnessSignature:
    """Xert-style fitness signature describing an athlete's power profile.

    Attributes:
        ftp:        Functional Threshold Power (watts) — sustainable ~1hr power.
        ltp:        Lower Threshold Power (watts) — ~75% of FTP, aerobic threshold.
        hie:        High Intensity Energy (kJ) — anaerobic work capacity above FTP.
                    NOTE: kept for backward compatibility; for the v1.0.6 3D
                    strain-score model use ``wprime_j`` (joules) instead.
        peak_power: Peak Power (watts) — maximal 1-5 second sprint power.
        cp_w:       v1.0.6+ Critical Power (watts) — Monod 2-param fit value
                    when available; ``None`` => downstream code falls back to
                    ``int(ftp * 1.03)`` (McGrath 2021 approximation).
        wprime_j:   v1.0.6+ W' anaerobic work capacity (joules) — Monod fit
                    value; ``None`` => fallback to ``hie * 1000``.
        pmax_w:     v1.0.6+ maximum instantaneous power (watts) — best 5 s
                    or ICU sportInfo[0].pMax; ``None`` => downstream code
                    falls back to ``int(ftp * 1.30)`` (Coggan 2-min approx).
    """
    ftp: int
    ltp: int
    hie: float
    peak_power: int
    cp_w: int | None = None
    wprime_j: float | None = None
    pmax_w: int | None = None


# ══════════════════════════════════════════════════════════════════════════════
# BEST-EFFORT EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def extract_best_efforts(
    samples: list[RideSample],
    timestamps: list[float] | None = None,
) -> dict[int, int]:
    """Compute maximum mean power for standard durations from 1Hz ride data.

    Uses a sliding-window approach over the power stream to find the highest
    average power sustained for each standard duration (5s, 30s, 1min, 5min,
    8min, 20min, 30min, 60min).

    The sliding window assumes a uniform 1 Hz sample rate with no gaps. When
    ``timestamps`` is supplied, the function validates monotonicity and that
    the mean inter-sample delta is ≈ 1 s; otherwise it emits a warning via
    the ``logging`` module and proceeds on the implicit assumption (legacy
    callers). Non-uniform or paused data will silently bias best efforts.

    Args:
        samples: Ordered list of 1Hz ride samples from a single ride.
        timestamps: Optional list of per-sample epoch seconds (same length as
            ``samples``). If given, checked for monotonicity and dt≈1.

    Returns:
        Dict mapping duration_seconds -> best_average_watts for each duration
        that has enough data. Durations longer than the ride are omitted.
    """
    if not samples:
        return {}

    if timestamps is not None:
        import logging
        log = logging.getLogger(__name__)
        if len(timestamps) != len(samples):
            log.warning(
                "extract_best_efforts: timestamps length (%d) != samples length (%d); "
                "skipping timestamp validation",
                len(timestamps), len(samples),
            )
        elif len(timestamps) >= 2:
            # Monotonicity
            for i in range(1, len(timestamps)):
                if timestamps[i] < timestamps[i - 1]:
                    log.warning(
                        "extract_best_efforts: non-monotonic timestamps at index %d "
                        "(%.3f → %.3f)", i, timestamps[i - 1], timestamps[i],
                    )
                    break
            # Average dt ≈ 1
            span = timestamps[-1] - timestamps[0]
            avg_dt = span / max(1, len(timestamps) - 1)
            if not (0.9 <= avg_dt <= 1.1):
                log.warning(
                    "extract_best_efforts: mean sample interval %.3fs differs from "
                    "expected 1Hz; best-effort windows will be biased", avg_dt,
                )
    else:
        import logging
        logging.getLogger(__name__).debug(
            "extract_best_efforts: no timestamps passed — assuming 1Hz, no gaps"
        )

    powers = [s.power for s in samples]
    n = len(powers)
    best_efforts: dict[int, int] = {}

    for duration in STANDARD_DURATIONS:
        if duration > n:
            continue

        # Sliding window: compute initial sum, then slide
        window_sum = sum(powers[:duration])
        best_sum = window_sum

        for i in range(1, n - duration + 1):
            window_sum += powers[i + duration - 1] - powers[i - 1]
            if window_sum > best_sum:
                best_sum = window_sum

        best_efforts[duration] = round(best_sum / duration)

    return best_efforts


# ══════════════════════════════════════════════════════════════════════════════
# FTP AUTO-ESTIMATION
# ══════════════════════════════════════════════════════════════════════════════

def estimate_ftp(best_efforts: dict[int, int]) -> Optional[int]:
    """Estimate FTP from best-effort power data, preferring longer durations.

    Applies standard scaling factors to each qualifying effort. Prefers
    LONGER-duration efforts because the FTP↔time-to-exhaustion relationship
    is better anchored at 20–60 minutes; short (5–8 min) efforts are more
    variable (VO2max-limited). Iteration runs from longest to shortest so
    that longer-duration efforts seed ``best`` first; shorter efforts can
    only establish a result if no long-duration effort was available, and
    long-duration efforts can displace each other when they read higher.

    Args:
        best_efforts: Dict mapping duration_seconds -> best_average_watts.
                      Typically produced by extract_best_efforts().

    Returns:
        Best FTP estimate in watts, or None if no effort >= 5 minutes exists.
    """
    durations = sorted(FTP_SCALING_FACTORS.keys(), reverse=True)
    best: tuple[int, float] | None = None  # (duration_sec, estimate_watts)

    for duration in durations:
        if duration not in best_efforts:
            continue
        if duration < MIN_FTP_EFFORT_DURATION:
            continue

        factor = FTP_SCALING_FACTORS[duration]
        watts = best_efforts[duration]
        estimated = watts * factor

        if best is None:
            best = (duration, estimated)
        elif duration >= 1200 and estimated > best[1]:
            # Prefer long-duration estimates when they read higher — they
            # come from steadier, more FTP-like efforts.
            best = (duration, estimated)

    return round(best[1]) if best else None


# ══════════════════════════════════════════════════════════════════════════════
# FITNESS SIGNATURE COMPUTATION
# ══════════════════════════════════════════════════════════════════════════════

def compute_fitness_signature(
    best_efforts: dict[int, int],
    ftp: int,
) -> FitnessSignature:
    """Compute a Xert-style fitness signature from best efforts and FTP.

    Args:
        best_efforts: Dict mapping duration_seconds -> best_average_watts.
        ftp: Functional Threshold Power in watts (known or estimated).

    Returns:
        FitnessSignature with FTP, LTP, HIE, and Peak Power populated.

    Raises:
        ValueError: If FTP <= 0 or required best-effort data is missing.
    """
    if ftp <= 0:
        raise ValueError(f"FTP must be positive, got {ftp}")

    # LTP: Lower Threshold Power — aerobic threshold approximation (Coggan)
    ltp = round(ftp * 0.75)

    # HIE: High Intensity Energy — anaerobic capacity above FTP
    # Simplified: HIE = (best_5min_watts - FTP) * 300s / 1000 (convert J to kJ)
    best_5min = best_efforts.get(300)
    if best_5min is not None and best_5min > ftp:
        hie = round((best_5min - ftp) * 300 / 1000, 1)
    else:
        # Default estimate: ~20 kJ is typical for trained cyclists
        # (80·FTP/1000 ≈ 20 kJ at FTP=250W).
        hie = round(ftp * 80.0 / 1000, 1)  # conservative fallback

    # Peak Power: best 5-second power
    peak_power = best_efforts.get(5, 0)
    if peak_power == 0:
        # Fallback: estimate from shorter available data or use 2x FTP
        peak_power = round(ftp * 2.0)

    return FitnessSignature(
        ftp=ftp,
        ltp=ltp,
        hie=hie,
        peak_power=peak_power,
    )


# ══════════════════════════════════════════════════════════════════════════════
# FTP-TEST FORMULAS (v4.1.0 / T2)
# ══════════════════════════════════════════════════════════════════════════════

def coggan_20min_ftp(power_series: list[int] | list[float]) -> dict | None:
    """T2 — Coggan 20-min FTP from a ride's 1Hz power series.

    Formula: ``suggested_ftp = 0.95 * mean_power(best_20min_segment)`` (exact
    1200-s window, arithmetic MEAN — never NP). Returns None when the ride is
    shorter than 20 minutes. A5 (v3.2.0): the max-mean window's final 30 s is
    capped at that window's own p90 before meaning, removing a finish kick
    without ever selecting a lower plateau. Additive advisories (never block):
    ``pacing_drift`` / ``pacing_drift_pct`` when first-vs-second-half drift
    > 8%, and ``blowout_missing`` when no ≥4-min ≥105% effort precedes the
    block in the prior ~15 min. Returns::

        {
          "type": "coggan_20min",
          "value": int,              # suggested FTP (watts)
          "best_20min": int,         # best 20-min mean power (watts)
          "formula_used": "0.95 * best_20min_mean",
          # optional: pacing_drift, pacing_drift_pct, blowout_missing
        }
    """
    if not power_series:
        return None
    n = len(power_series)
    if n < 1200:
        return None
    powers = [float(p or 0) for p in power_series]
    window = 1200  # 20 min @ 1 Hz
    # Locate the MAX-mean 1200-s window (finds the test block). A5 (v3.2.0):
    # keep THIS window — the flattest-window search under-reads (proven) — but
    # cap its final 30 s at the window's OWN p90 before meaning, so a finish
    # kick can't inflate the mean while a genuine lower plateau is never
    # substituted (a cap can only lower the tail, never pick a different block).
    cur = sum(powers[:window])
    best = cur
    best_idx = 0
    for i in range(1, n - window + 1):
        cur += powers[i + window - 1] - powers[i - 1]
        if cur > best:
            best = cur
            best_idx = i
    seg = powers[best_idx:best_idx + window]
    # p90 of the window (linear-interpolated), used to clip the finish tail.
    srt = sorted(seg)
    rank = 0.90 * (len(srt) - 1)
    lo_i = int(rank)
    frac = rank - lo_i
    p90 = srt[lo_i] + (srt[min(lo_i + 1, len(srt) - 1)] - srt[lo_i]) * frac
    capped = seg[:]
    for k in range(window - 30, window):
        if capped[k] > p90:
            capped[k] = p90
    best_mean = sum(capped) / window  # arithmetic MEAN (NOT NP) — pinned

    out = {
        "type": "coggan_20min",
        "value": int(round(best_mean * 0.95)),
        "best_20min": int(round(best_mean)),
        "formula_used": "0.95 * best_20min_mean",
    }

    # Advisories (ADDITIVE, never block). Built off the RAW window `seg`.
    # Pacing drift: first-half vs second-half mean of the block > 8%.
    first_half = sum(seg[:window // 2]) / (window // 2)
    second_half = sum(seg[window // 2:]) / (window - window // 2)
    if first_half > 0:
        drift = abs(first_half - second_half) / first_half
        if drift > 0.08:
            out["pacing_drift_pct"] = round(drift * 100.0, 1)
            out["pacing_drift"] = True
    # Blow-out presence: a ≥4-min (240 s) effort ≥ 105% of the block mean in
    # the ~15 min (900 s) BEFORE the block. Absent ⇒ blowout_missing advisory.
    block_mean_raw = sum(seg) / window
    pre_lo = max(0, best_idx - 900)
    pre = powers[pre_lo:best_idx]
    blowout_present = False
    if len(pre) >= 240:
        w4 = 240
        c = sum(pre[:w4])
        if c / w4 >= 1.05 * block_mean_raw:
            blowout_present = True
        else:
            for i in range(1, len(pre) - w4 + 1):
                c += pre[i + w4 - 1] - pre[i - 1]
                if c / w4 >= 1.05 * block_mean_raw:
                    blowout_present = True
                    break
    if not blowout_present:
        out["blowout_missing"] = True

    return out


def ramp_test_ftp(
    power_series: list[int] | list[float],
    pm=None,
) -> dict | None:
    """T2 — Ramp-test FTP from a ride's 1Hz power series.

    Formula: ``suggested_ftp = 0.75 * mean_power(best_sustained_60s)`` — the
    calc TRUSTS ``detect_ftp_test_shape`` already confirmed a ramp. ONE
    flatness guard keeps a warmup/cooldown spike-and-coast minute from winning:
    a 60-s window only counts when its mean ≥ 0.70 × its own PEAK 1-s power. A
    flat ramp step passes (≈1.0); a 30-s-sprint-then-coast minute fails (≈0.56).

    Aborted-ramp self-protection is preserved: the best sustained MINUTE is the
    scoring quantity, so quitting early scores the last full step reached.

    GF4 advisory (high-W′ / sprinter over-read): when a TRUSTWORTHY measured
    Pmax is set (``capacity_cap.pmax_is_set``) and measured Pmax/FTP ≥ 1.35,
    attach ``factor_band=[0.72, 0.77]`` + ``likely_overestimate=True``. This is
    ADVISORY ONLY — the value stays ``round(0.75 × best_60s)`` (no W′ math, the
    number is never auto-changed).
    """
    if not power_series:
        return None
    n = len(power_series)
    if n < 60:
        return None
    powers = [float(p or 0) for p in power_series]

    window = 60
    best = None
    cur = sum(powers[:window])
    for i in range(0, n - window + 1):
        if i > 0:
            cur += powers[i + window - 1] - powers[i - 1]
        mean_i = cur / window
        # ponytail: flatness guard rejects spike-and-coast; a real ramp step is
        # near-constant, so mean ≥ 0.70×peak. A warmup/cooldown sprint minute
        # (30s hard + 30s coast) has mean ≈ 0.56×peak and can never win.
        # Grill F3: "peak" is the window's p95, not its absolute 1-s max — a
        # single 800 W meter artifact inside a flat top step made every window
        # covering that step fail the guard and dropped the score a full step;
        # a genuine 30-s sprint still dominates p95 and is still rejected.
        srt_i = sorted(powers[i:i + window])
        peak_i = srt_i[int(0.95 * (window - 1))]
        if peak_i > 0 and mean_i < 0.70 * peak_i:
            continue
        if best is None or mean_i > best:
            best = mean_i

    if best is None:
        # No sustained 60-s window (every minute was spiky) — fall back to the
        # plain global best-60s so a detected ramp never returns None.
        cur = sum(powers[:window])
        best = cur / window
        for i in range(1, n - window + 1):
            cur += powers[i + window - 1] - powers[i - 1]
            if cur / window > best:
                best = cur / window

    best_mean = best
    out = {
        "type": "ramp",
        "value": int(round(best_mean * 0.75)),
        "best_60s": int(round(best_mean)),
        "formula_used": "0.75 * best_60s_mean",
    }

    # GF4: high measured-Pmax over-read advisory (advisory only, value fixed).
    try:
        import capacity_cap
        if pm is not None and capacity_cap.pmax_is_set(pm):
            ftp = float(getattr(pm, "ftp", 0) or 0)
            pmax = float(getattr(pm, "pmax_w", 0) or 0)
            if ftp > 0 and pmax > 0 and (pmax / ftp) >= 1.35:
                out["factor_band"] = [0.72, 0.77]
                out["likely_overestimate"] = True
    except Exception:
        pass

    return out


def sixty_min_ftp(power_series: list[int] | list[float]) -> dict | None:
    """T2 — 60-min (hour-of-power) FTP from a ride's 1Hz power series.

    Formula: ``suggested_ftp = 1.00 * mean_power(detected_plateau)`` — FTP is
    DEFINED as best-hour power, so the factor is 1.0 by definition, but the
    scored extent is the DETECTED plateau, never a blind best-3600s window:
    the protocol's most common failure is quitting early, and averaging a
    fixed 60-min window across the quit point silently under-reads the
    effort while still stamping it "sixty_min".

    Plateau extraction: best 20-min core (minute ladder), grown left/right
    while each adjacent minute stays ≥ 0.90 × the core mean. A plateau
    shorter than 50 min means the hour was not completed → returns None (the
    caller falls back to ``coggan_20min_ftp`` on the same series, which is
    validated and conservative for a 20–50-min effort).

    Additive advisory (never blocks): ``pacing_drift``/``pacing_drift_pct``
    when first-vs-second-half drift of the plateau > 8%.
    """
    if not power_series:
        return None
    powers = [float(p or 0) for p in power_series]
    minutes = _minute_averages(powers)
    core_w = 20
    if len(minutes) < 50:
        return None
    cur = sum(minutes[:core_w])
    best = cur
    best_idx = 0
    for k in range(1, len(minutes) - core_w + 1):
        cur += minutes[k + core_w - 1] - minutes[k - 1]
        if cur > best:
            best = cur
            best_idx = k
    core_mean = best / core_w
    if core_mean <= 0:
        return None
    lo = best_idx
    hi = best_idx + core_w  # exclusive
    # Grill F1: bridge short interruptions. A completed hour with a 2-min
    # traffic-light dip used to split the plateau at the dip (→ None → the
    # coggan fallback under-read a finished test). A gap of up to 3
    # consecutive sub-threshold minutes is crossed when the effort resumes
    # beyond it; longer gaps still end the plateau (rest periods).
    thresh = 0.90 * core_mean

    def _grow(idx: int, step: int) -> int:
        # idx = next candidate minute in `step` direction; returns final
        # boundary (lo: inclusive index, hi: exclusive index semantics are
        # handled by the callers' offsets).
        while 0 <= idx < len(minutes):
            if minutes[idx] >= thresh:
                idx += step
                continue
            bridged = False
            for gap in range(1, 4):
                j = idx + gap * step
                if 0 <= j < len(minutes) and minutes[j] >= thresh:
                    idx = j + step
                    bridged = True
                    break
            if not bridged:
                break
        return idx

    lo = _grow(lo - 1, -1) + 1
    hi = _grow(hi, 1)
    plateau_min = hi - lo
    if plateau_min < 50:
        return None
    seg = powers[lo * 60:hi * 60]
    plateau_mean = sum(seg) / len(seg)
    out = {
        "type": "sixty_min",
        "value": int(round(plateau_mean)),
        "best_60min": int(round(plateau_mean)),
        "plateau_min": plateau_min,
        "formula_used": "1.00 * plateau_mean",
    }
    half = len(seg) // 2
    first_half = sum(seg[:half]) / half
    second_half = sum(seg[half:]) / (len(seg) - half)
    if first_half > 0:
        drift = abs(first_half - second_half) / first_half
        if drift > 0.08:
            out["pacing_drift_pct"] = round(drift * 100.0, 1)
            out["pacing_drift"] = True
    return out


def _minute_averages(powers: list[float]) -> list[float]:
    """1-minute mean-power ladder for the whole series (drops the ragged tail
    minute so every entry is a full 60-s average)."""
    n = len(powers)
    return [sum(powers[i * 60:(i + 1) * 60]) / 60.0 for i in range(n // 60)]


def _hint_test_kind(filename_hint) -> "str | None":
    """FTP-tests grill (HINT hardening) — recognise a protocol from name/file
    hints. Accepts a single string or a list of independent source strings
    (file name, FIT workout name, ICU activity name). Each source is
    normalized (every separator run → "_") and must START with the test
    prefix — a title that merely MENTIONS a test ("skipped ftp test ramp
    today, easy spin") must never route the ride into scoring, while the
    shipped names ("FTP Test — Coggan 20min protocol (59min)") and files
    (ftp_test_coggan_….zwo) all begin with it."""
    if not filename_hint:
        return None
    sources = (filename_hint if isinstance(filename_hint, (list, tuple))
               else [filename_hint])
    for src in sources:
        if not src:
            continue
        lname = re.sub(r"[^a-z0-9]+", "_", str(src).lower()).strip("_")
        if not lname.startswith("ftp_test"):
            continue
        if "ftp_test_coggan" in lname:
            return "coggan_20min"
        if "ftp_test_ramp" in lname:
            return "ramp"
        if "ftp_test_60min" in lname:
            return "sixty_min"
    return None


def detect_ftp_test_shape(
    power_series: list[int] | list[float],
    filename_hint=None,
) -> str | None:
    """T1 — classify a ride's power profile as Coggan 20-min, Ramp, or not a test.

    DETECTION IS STRUCTURED-WORKOUT-PRIMARY (v3.2.0, D3): the filename/tag
    path is authoritative and returns FIRST — this is exactly how Zwift / TR
    know a ride is a test (they ran the structured file). Since 3.2.0 ships
    the tests as tagged workouts, this is the normal path.

    The power-shape heuristic is a CONSERVATIVE fallback for free-form rides:
      * Ramp: a LATE-PEAKING monotone climb — the ride's best-60s sits in the
        last third AND the last-third mean is ≥ 1.5× the first-third mean. A
        4×8 threshold ride (peaks spread throughout, first-third ≈ last-third)
        fails both ⇒ not a ramp.
      * Coggan: a single sustained ≥18-min plateau ≥ 1.15× ride-mean with NO
        second comparable plateau (rejects 2×20) and a blow-out shape present.

    Ambiguous → None (never auto-score a random hard ride).

    Returns "coggan_20min", "ramp", or None.
    """
    hint_kind = _hint_test_kind(filename_hint)
    if hint_kind:
        return hint_kind

    if not power_series:
        return None
    n = len(power_series)
    powers = [float(p or 0) for p in power_series]

    # Ramp heuristic. Grill FP-2: "best-60s late + 1.5× thirds ratio" alone
    # fires on ANY easy-start/hard-finish ride (a late 15-min climb at steady
    # power scored as a ramp, suggesting 0.75× climb power). A real ramp is a
    # MONOTONE CLIMB: require (c) a run of ≥8 consecutive non-decreasing
    # minute-means ending at the best minute with a ≥40% net rise across the
    # run — a steady block is flat (run length 1–2) and fails — and (d) ride
    # short enough to be a ramp at all (≤60 min; the shipped protocol is 35).
    if 180 <= n <= 3600:
        third = n // 3
        first_mean = sum(powers[:third]) / third
        last_mean = sum(powers[2 * third:]) / (n - 2 * third)
        # best-60s location
        window = 60
        cur = sum(powers[:window])
        best = cur
        best_idx = 0
        for i in range(1, n - window + 1):
            cur += powers[i + window - 1] - powers[i - 1]
            if cur > best:
                best = cur
                best_idx = i
        best_in_last_third = best_idx >= 2 * third
        if (best_in_last_third and first_mean > 0
                and last_mean >= 1.5 * first_mean):
            minutes = _minute_averages(powers)
            m_best = min(best_idx // 60, len(minutes) - 1)
            j = m_best
            # Extend the climb backwards: each backward step may neither rise
            # (>5 W would mean power FELL going forward) nor drop more than a
            # plausible ramp step (40 W) — a flat block preceded by easy
            # riding has a >40 W cliff at its start and the run stops there.
            while j > 0:
                d = minutes[j] - minutes[j - 1]
                if d < -5 or d > 40:
                    break
                j -= 1
            run_len = m_best - j + 1
            run_base = max(minutes[j], 1.0)
            if run_len >= 8 and minutes[m_best] >= 1.4 * run_base:
                return "ramp"

    # Sixty-min heuristic — checked BEFORE Coggan because a 60-min plateau
    # contains an 18-min one (order is load-bearing). Guards keep an ordinary
    # steady endurance hour out: (a) best 50-min plateau ≥ 1.10× ride-mean —
    # a Z2 hour with a token warmup has plateau ≈ ride-mean and fails; (b)
    # absolute floor 150 W; (c) the non-plateau share of the ride ≤ 45% — a
    # long ride that merely CONTAINS a hard hour stays un-scored (never
    # auto-score a random hard ride; the FIT-import path with the file hint
    # is how a deliberate hour test is normally recognised). 45% because the
    # share is measured against the 50-min WINDOW, not the true plateau: the
    # shipped 86-min protocol ride itself is 42% non-window.
    if n >= 3000:  # ≥50 min
        ride_mean = sum(powers) / n
        minutes = _minute_averages(powers)
        pw = 50
        if len(minutes) >= pw and ride_mean > 0:
            cur = sum(minutes[:pw])
            best = cur
            best_k = 0
            for k in range(1, len(minutes) - pw + 1):
                cur += minutes[k + pw - 1] - minutes[k - 1]
                if cur > best:
                    best = cur
                    best_k = k
            best_mean = best / pw
            # Grill FP-1/FP-3: the window must be a FLAT plateau, not merely
            # hot on average. Rest-broken interval rides (2×20, 3×15) satisfy
            # the 1.10× uplift because the rests drag ride_mean down, and a
            # progressive ride's rising back half does too — but both have
            # many window minutes far below the window mean. A genuine hour
            # of power has at most a brief traffic-light dip: allow ≤3
            # sub-0.85× minutes inside the best window.
            low_minutes = sum(
                1 for m in minutes[best_k:best_k + pw]
                if m < 0.85 * best_mean)
            if (best_mean >= 1.10 * ride_mean and best_mean >= 150
                    and (n - pw * 60) <= 0.45 * n
                    and low_minutes <= 3):
                return "sixty_min"

    # Coggan heuristic (TIGHTENED, v3.2.0): a single sustained ≥18-min plateau
    # meaningfully above ride-mean, with NO second comparable plateau elsewhere
    # (that would be a 2×20 / intervals ride, not a single-block Coggan test),
    # and a blow-out effort present in the run-up (the mandatory 5-min
    # depletion). Ambiguous shapes fall through to None.
    if n >= 1080:  # need ≥18 min for the plateau
        ride_mean = sum(powers) / n
        minutes = _minute_averages(powers)
        # Best sustained 18-min plateau mean + its location.
        pw = 18
        if len(minutes) >= pw:
            cur = sum(minutes[:pw])
            best = cur
            best_idx = 0
            for k in range(1, len(minutes) - pw + 1):
                cur += minutes[k + pw - 1] - minutes[k - 1]
                if cur > best:
                    best = cur
                    best_idx = k
            best_mean = best / pw
            if best_mean >= 1.15 * ride_mean and best_mean >= 150:
                # Reject a SECOND comparable ≥18-min plateau that does not
                # overlap the first (2×20 / repeat-block ride).
                second = False
                for k in range(0, len(minutes) - pw + 1):
                    if k + pw <= best_idx or k >= best_idx + pw:  # disjoint
                        seg_mean = sum(minutes[k:k + pw]) / pw
                        if seg_mean >= 0.95 * best_mean:
                            second = True
                            break
                # Blow-out shape: a ≥4-min effort ≥ 1.05× the plateau mean
                # somewhere OUTSIDE the plateau (the depletion / VO2 spike).
                blowout = False
                for k in range(0, len(minutes) - 4 + 1):
                    if k + 4 <= best_idx or k >= best_idx + pw:
                        seg4 = sum(minutes[k:k + 4]) / 4
                        if seg4 >= 1.05 * best_mean:
                            blowout = True
                            break
                if not second and blowout:
                    return "coggan_20min"
    return None


def evaluate_ftp_test(power_series, cadence_series=None,
                      filename_hint: "str | None" = None,
                      prior_ftp: int = 0) -> "dict | None":
    """FTP-tests IP W2b — THE single entry point for scoring a ride as an FTP
    test. Both ingestion paths (FIT import in app._parse_fit_stats, ICU
    sync-path detection in power_curve's backfill) call this, so recognition
    and calculation can never drift between them.

    Returns None when the ride is not a recognisable test, else::

        {"is_ftp_test": True, "ftp_test_type": kind,
         "ftp_test_suggestion": {...}, "ftp_test_halted": bool,
         "ftp_test_halt_step": int | None}

    The suggestion carries prior_ftp / pct_delta for the review modal, the
    formula used, per-protocol extras (best_20min / best_60s / best_60min /
    plateau_min), the additive advisories (pacing_drift, ramp factor_band),
    and — for an abandoned hour (plateau < 50 min) — ``fallback_from:
    "sixty_min"`` with the validated 20-min method's number instead of a
    blind 60-min average.
    """
    # Grill F5: FIT/stream corruption armour. uint16 invalid-value spikes
    # (65535) and sign-corrupted samples are zeroed BEFORE any windowing —
    # three single 65535 samples inside a clean 250 W block otherwise
    # inflated the suggestion by +57%.
    if power_series:
        power_series = [
            0 if (p is None or p < 0 or p > 2500) else p
            for p in power_series
        ]
    kind = detect_ftp_test_shape(power_series, filename_hint=filename_hint)
    if not kind:
        return None
    fallback = False
    if kind == "coggan_20min":
        s = coggan_20min_ftp(power_series)
    elif kind == "ramp":
        s = ramp_test_ftp(power_series)
    else:
        s = sixty_min_ftp(power_series)
        if not s:
            # Grill FP-1: the early-quit fallback is legitimate ONLY when the
            # rider demonstrably RODE the 60-min protocol (file/name hint) and
            # abandoned it. When "sixty_min" came from the shape heuristic,
            # a missing plateau means the heuristic was wrong about the ride
            # (rest-broken intervals, progressive rides) — scoring those with
            # the coggan calculator bypassed its own 2×20 rejection. Silence.
            if _hint_test_kind(filename_hint) == "sixty_min":
                s = coggan_20min_ftp(power_series)
                fallback = bool(s)
    if not s:
        return None
    val = int(s["value"])
    # Grill F5 (2nd layer): refuse a physiologically impossible suggestion —
    # corrupt streams must never reach the review modal with FTP 62258.
    if not (50 <= val <= 600):
        return None
    prior = int(prior_ftp or 0)
    delta = round((val - prior) / prior * 100.0, 1) if prior > 0 else 0.0
    method = "coggan_20min" if (kind == "sixty_min" and fallback) else kind
    sug = {
        "type": kind,
        "method": method,
        "ftp": val,
        "value": val,
        "prior_ftp": prior,
        "pct_delta": delta,
        "source": f"tested_{kind}",
        "formula_used": s["formula_used"],
    }
    for k in ("best_20min", "best_60s", "best_60min", "plateau_min",
              "pacing_drift", "pacing_drift_pct", "blowout_missing",
              "factor_band", "likely_overestimate"):
        if s.get(k) is not None:
            sug[k] = s[k]
    if fallback:
        sug["fallback_from"] = "sixty_min"
    out = {
        "is_ftp_test": True,
        "ftp_test_type": kind,
        "ftp_test_suggestion": sug,
        "ftp_test_halted": False,
        "ftp_test_halt_step": None,
    }
    if kind == "ramp" and cadence_series:
        halt = detect_ramp_halt(power_series, cadence_series)
        if halt and halt.get("halted"):
            out["ftp_test_halted"] = True
            out["ftp_test_halt_step"] = halt.get("halt_at_step")
    return out


def detect_ramp_halt(
    power_series: list[int] | list[float],
    cadence_series: list[int] | list[float],
    step_seconds: int = 60,
    step_increment_pct: float = 0.06,
    start_pct: float = 0.56,
    ref_ftp: int | None = None,
) -> dict | None:
    """T3 — post-hoc ramp auto-halt detector.

    Scans the ride for 3+ consecutive seconds where:
      cadence < 50 AND power < 85% of target-for-that-step.

    ``ref_ftp`` anchors the step targets; if omitted, reconstructs them
    from the standard Ramp template (start_pct=0.56 FTP, +6%/min). When
    ref_ftp is unknown we estimate it from the power_series max-in-first-
    5min as a weak proxy (≈0.56 FTP) so the absolute halt threshold still
    makes sense.

    Returns ``{halted: bool, halt_at_step: int, halt_at_sec: int}`` or None
    when the ride is too short to evaluate.
    """
    if not power_series or not cadence_series:
        return None
    n = min(len(power_series), len(cadence_series))
    if n < step_seconds * 3:
        return None
    if ref_ftp is None or ref_ftp <= 0:
        # Weak proxy: the steady-state at 5-6 min is around 0.86 FTP (step 6).
        # Take the average of minute 5's power samples and divide by 0.86 to
        # normalize back to ~FTP. Still rough but better than hard-code.
        if n >= 360:
            mean_min5 = sum(float(p or 0) for p in power_series[300:360]) / 60
            if mean_min5 > 30:
                ref_ftp = int(mean_min5 / 0.86)
    if not ref_ftp or ref_ftp <= 0:
        return None

    # Grill F4 (a): anchor step 0 at the detected ramp START, not ride second
    # 0. The shipped protocol has a 5-min 0.50-FTP warmup before the 0.56
    # first step; counting from the file start over-reported every halt step
    # by exactly the warmup minutes. Leading minutes clearly below the first
    # step's band are warmup.
    minutes = _minute_averages([float(p or 0) for p in power_series[:n]])
    warmup_min = 0
    first_step_w = ref_ftp * start_pct
    for m in minutes:
        if m < 0.90 * first_step_w:
            warmup_min += 1
        else:
            break
    warmup_s = warmup_min * 60
    # Grill F4 (b): stop scanning once the reconstructed target exceeds what
    # the rider demonstrably produced (best 60 s) — past that point the ride
    # is post-exhaustion coast/cooldown, which the cadence+power rule would
    # otherwise re-flag as a "halt" far beyond the real quit step.
    best60 = 0.0
    if n >= 60:
        cur = sum(float(p or 0) for p in power_series[:60])
        best60 = cur
        for i in range(1, n - 60 + 1):
            cur += float(power_series[i + 59] or 0) - float(power_series[i - 1] or 0)
            if cur > best60:
                best60 = cur
        best60 /= 60.0

    low_streak = 0
    for i in range(warmup_s, n):
        step_idx = (i - warmup_s) // step_seconds
        target_pct = start_pct + step_idx * step_increment_pct
        target_w = ref_ftp * target_pct
        if best60 > 0 and target_w > 1.05 * best60:
            break
        p = float(power_series[i] or 0)
        c = float(cadence_series[i] or 0)
        if p < target_w * 0.85 and c < 50:
            low_streak += 1
            if low_streak >= 3:
                return {
                    "halted": True,
                    "halt_at_step": step_idx + 1,
                    "halt_at_sec": i,
                }
        else:
            low_streak = 0
    return {"halted": False, "halt_at_step": 0, "halt_at_sec": 0}


# ══════════════════════════════════════════════════════════════════════════════
# MONOD-SCHERRER 2-PARAMETER CP/W' FIT (v3.6.0-fix26 §4.2)
# ══════════════════════════════════════════════════════════════════════════════

# Durations (seconds) the Monod-Scherrer linear fit accepts. Durations
# shorter than 3 min are VO2max-dominated and bias W' upward; longer than
# 20 min drift into FTP territory. Clip to [180, 1200] s per Poole et al.
# 2016 (Med Sci Sports Exerc 48(11):2320-34) canonical CP test protocol.
MONOD_DURATIONS_S = (180, 300, 600, 1200)

# Minimum number of valid best-effort points required to fit. Two-param
# linear regression needs ≥2 points, but the R² gate is unstable at n=2
# (perfect fit by construction) so require ≥3 durations.
MONOD_MIN_POINTS = 3

# R² floor below which the fit is rejected — means the athlete's power
# duration curve is not well-approximated by the linear hyperbolic model
# (common when a single effort dominates, or efforts came from different
# fitness states). Per Poole 2016 CP test reliability papers, R² ≥ 0.9
# is the accepted threshold.
MONOD_R2_MIN = 0.90


def compute_cp_wprime(
    best_efforts: dict[int, int],
) -> tuple[float, float] | None:
    """Monod-Scherrer 2-parameter linear fit on ``P = W'/t + CP``.

    Performs ordinary least squares on points ``(1/t, P)`` where t is
    duration in seconds and P is best-average power in watts. The slope
    of the fit is W' (joules), the intercept is CP (watts).

    Args:
        best_efforts: Dict of {duration_s: power_w} pairs. Typically from
            `extract_best_efforts()`, containing the standard
            {180, 300, 600, 1200} s durations. Durations outside this set
            are filtered out (too short = VO2max artefact, too long =
            FTP-bound).

    Returns:
        ``(cp_w, wprime_j)`` tuple on success, or ``None`` when:

        * Fewer than ``MONOD_MIN_POINTS`` (3) valid durations are present.
        * Fit R² is below ``MONOD_R2_MIN`` (0.90) — the athlete's power
          curve isn't well-described by the linear Monod model.
        * Computed CP ≤ 0 or W' ≤ 0 (physiologically impossible — usually
          caused by inconsistent efforts taken from different fitness
          states or data corruption).
        * Values fall outside the profile validators' physiological
          ranges: CP [100, 500] W, W' [5000, 40000] J (matches
          ProfileManager.save_athlete validators).

    The caller is responsible for gating writes by profile source
    priority — use ``ProfileManager._set_wprime(value, "monod")`` which
    refuses to overwrite "manual" or "icu" values.

    References:
        Monod H & Scherrer J (1965). "The work capacity of a synergic
        muscular group." Ergonomics 8:329-38.
        Poole et al. (2016). "Critical Power: an important fatigue
        threshold in exercise physiology." MSSE 48(11):2320-34.
    """
    if not best_efforts:
        return None

    # Collect (x=1/t, y=P) pairs for valid durations only.
    points: list[tuple[float, float]] = []
    for dur in MONOD_DURATIONS_S:
        if dur not in best_efforts:
            continue
        p = best_efforts[dur]
        if p is None or p <= 0:
            continue
        points.append((1.0 / dur, float(p)))

    if len(points) < MONOD_MIN_POINTS:
        log.debug(
            "compute_cp_wprime: only %d valid points (need %d)",
            len(points), MONOD_MIN_POINTS,
        )
        # v3.6.0-fix30-logs-ext: structured emission — even the "not enough
        # points" branch goes out on the log so post-mortem can distinguish
        # "never attempted" from "attempted and rejected".
        log_power.info(
            f"EVENT=monod_fit cp_w=None wprime_j=None r2=None "
            f"n_points={len(points)} result=insufficient"
        )
        return None

    # OLS linear regression on (x=1/t, y=P):
    #   y = slope * x + intercept
    #   slope     = W'  (joules) — coefficient on 1/t
    #   intercept = CP  (watts)  — asymptotic sustainable power
    n = len(points)
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    denom = n * sxx - sx * sx
    if denom <= 1e-12:
        log.debug("compute_cp_wprime: degenerate x-values; no spread in 1/t")
        return None
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n

    # Coefficient of determination R² = 1 - SSres / SStot.
    y_mean = sy / n
    ss_tot = sum((p[1] - y_mean) ** 2 for p in points)
    ss_res = sum((p[1] - (slope * p[0] + intercept)) ** 2 for p in points)
    if ss_tot <= 1e-12:
        # Flat y — caller probably has identical power across durations,
        # which is an artefact not a fit.
        return None
    r_squared = 1.0 - ss_res / ss_tot
    if r_squared < MONOD_R2_MIN:
        log.debug(
            "compute_cp_wprime: R²=%.3f < %.2f; rejecting fit",
            r_squared, MONOD_R2_MIN,
        )
        log_power.info(
            f"EVENT=monod_fit cp_w={intercept:.1f} wprime_j={slope:.0f} "
            f"r2={r_squared:.3f} n_points={len(points)} result=low_r2"
        )
        return None

    cp = intercept
    wprime = slope
    if cp <= 0 or wprime <= 0:
        log.debug(
            "compute_cp_wprime: non-physical fit (CP=%.1f W, W'=%.0f J)",
            cp, wprime,
        )
        log_power.info(
            f"EVENT=monod_fit cp_w={cp:.1f} wprime_j={wprime:.0f} "
            f"r2={r_squared:.3f} n_points={len(points)} result=non_physical"
        )
        return None

    # Profile validators will reject out-of-range values; gate here so the
    # caller can fall back to `ftp*80` without a ValueError round-trip.
    if not (100.0 <= cp <= 500.0):
        log.debug("compute_cp_wprime: CP=%.1f W out of [100, 500]", cp)
        log_power.info(
            f"EVENT=monod_fit cp_w={cp:.1f} wprime_j={wprime:.0f} "
            f"r2={r_squared:.3f} n_points={len(points)} result=cp_out_of_range"
        )
        return None
    if not (5000.0 <= wprime <= 40000.0):
        log.debug(
            "compute_cp_wprime: W'=%.0f J out of [5000, 40000]", wprime,
        )
        log_power.info(
            f"EVENT=monod_fit cp_w={cp:.1f} wprime_j={wprime:.0f} "
            f"r2={r_squared:.3f} n_points={len(points)} result=wprime_out_of_range"
        )
        return None

    log_power.info(
        f"EVENT=monod_fit cp_w={round(cp, 1)} wprime_j={round(wprime)} "
        f"r2={r_squared:.3f} n_points={len(points)} result=success"
    )
    return (round(cp, 1), round(wprime))


# ══════════════════════════════════════════════════════════════════════════════
# POST-HOC AEROBIC DECOUPLING (RIDE-REPORT RE-RENDER)
# ══════════════════════════════════════════════════════════════════════════════
#
# Live decoupling is computed inside `MetricsEngine.decoupling`
# (training_live.py:467) during a ride and persisted on
# `summary.decoupling_pct`. This helper recomputes the same metric from raw
# stored sample arrays so historical rides can be re-rendered without a live
# session — used by RIDE REPORT when the persisted value is missing.

def aerobic_decoupling(
    power_samples: list[int] | list[float],
    hr_samples: list[int] | list[float],
    sample_hz: int = 1,
) -> Optional[float]:
    """Pa:Hr drift over the ride (v3.6.0-fix25 canonical, §1.4 + §1.5).

    Unified with `MetricsEngine.decoupling` so live and post-hoc agree:
      1. Apply the canonical Z1 filter (50 ≤ power ≤ 2500, 60 ≤ HR ≤ 220).
      2. Trim the first ``DECOUPLING_WARMUP_TRIM_S`` seconds (15 min) of
         filtered samples — the warmup is where HR kinetics pull ef1 down
         and create spurious negative decoupling.
      3. Require ``DECOUPLING_MIN_FILTERED_S`` seconds (40 min) of
         post-trim samples; below that return None.
      4. Split the remaining samples in half; compute NP per half via
         the same 30-s rolling 4th-power mean the live engine uses,
         arithmetic mean HR per half.
      5. Return ``(ef1 − ef2) / ef1 * 100`` rounded to 1 dp. Positive =
         fatigue / HR drift, negative = HR suppression.

    Returns None when the ride is too short or the filters drop every
    sample — safe to substitute when re-rendering historical rides whose
    ``summary.decoupling_pct`` was lost.
    """
    from training_live import (
        _is_valid_decoupling_sample,
        DECOUPLING_WARMUP_TRIM_S,
        DECOUPLING_MIN_FILTERED_S,
    )

    if not power_samples or not hr_samples:
        return None
    n = min(len(power_samples), len(hr_samples))
    if n < 2:
        return None

    hz = max(1, int(sample_hz))
    # §1.4 canonical filter. Accumulate elapsed-time per kept sample so
    # the §1.5 warmup trim can run on actual seconds, not sample count.
    filtered_p: list[float] = []
    filtered_h: list[float] = []
    for i in range(n):
        p = power_samples[i]
        h = hr_samples[i]
        if h is None or p is None:
            continue
        if not _is_valid_decoupling_sample(p, h):
            continue
        filtered_p.append(float(p))
        filtered_h.append(float(h))

    if not filtered_p:
        return None

    # §1.5 warmup trim (samples-based because sample_hz is the given cadence).
    trim_samples = DECOUPLING_WARMUP_TRIM_S * hz
    if len(filtered_p) <= trim_samples:
        return None
    filtered_p = filtered_p[trim_samples:]
    filtered_h = filtered_h[trim_samples:]

    # §1.5 minimum post-trim duration: 40 min effective.
    if len(filtered_p) < DECOUPLING_MIN_FILTERED_S * hz:
        return None

    # NP per half — same 30-s rolling 4th-power mean used by the live engine.
    half = len(filtered_p) // 2
    p1 = filtered_p[:half]
    p2 = filtered_p[half:]
    h1 = filtered_h[:half]
    h2 = filtered_h[half:]

    def _np_for_half(powers: list[float]) -> float:
        from collections import deque
        win: deque[float] = deque(maxlen=30 * hz)
        p4_sum = 0.0
        count = 0
        for p in powers:
            win.append(p)
            if len(win) == win.maxlen:
                avg = sum(win) / len(win)
                p4_sum += avg ** 4
                count += 1
        if count == 0:
            return sum(powers) / len(powers) if powers else 0.0
        return (p4_sum / count) ** 0.25

    avg_h1 = sum(h1) / len(h1) if h1 else 0
    avg_h2 = sum(h2) / len(h2) if h2 else 0
    if avg_h1 <= 0 or avg_h2 <= 0:
        return None

    ef1 = _np_for_half(p1) / avg_h1
    ef2 = _np_for_half(p2) / avg_h2
    if ef1 <= 0:
        return None
    pct = round((ef1 - ef2) / ef1 * 100.0, 1)
    # v3.6.0-fix30-logs-ext: structured post-hoc decoupling emission so a
    # RIDE REPORT re-render lands a grep-able line. `valid_samples` is the
    # post-trim filtered length (the actual data that drove the halves).
    log_power.info(
        f"EVENT=aerobic_decoupling_computed pct={pct} method=np_per_half "
        f"valid_samples={len(filtered_p)}"
    )
    return pct
