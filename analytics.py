"""v1.8.0 — Training intensity distribution analytics.

References:
- Treff G, Winkert K, Sareban M, Steinacker JM, Sperlich B (2019).
  "The Polarization-Index: A Simple Calculation to Distinguish Polarized
  From Non-polarized Training Intensity Distributions."
  Frontiers in Physiology, 10:707. doi:10.3389/fphys.2019.00707
  https://www.frontiersin.org/articles/10.3389/fphys.2019.00707/full
- FastFitness.tips classification heuristic (also used by intervals.icu).

Domestique uses the FastFitness.tips formulation
``PI = log10((Z1+Z2 + Z5+) / Z3+Z4)`` — equivalent in spirit to Treff
(emphasises the easy/hard ratio over the moderate band) and matches the
single ``polarization_index`` value ICU reports on the activity GET, which
keeps the Domestique number consistent with what the user already sees in
intervals.icu.

Classification (v1.8.0 PI-BAND CASCADE):
PI-band rules per Treff 2019, replacing the centroid-distance heuristic so
the Treff reference ride (15/49.2/35.8) lands on `pyramidal` rather than the
old `hiit @ 1% confidence` mis-classification. Evaluate top-down, first match
wins. Centroid centres are retained only for confidence scoring.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

_log_dfa = logging.getLogger("domestique.analytics.dfa")


# Canonical centroids in (Z1+Z2 %, Z3+Z4 %, Z5+ %) space.
# Used for `classification_confidence` only; classification itself uses
# the PI-band cascade in `classify_distribution`.
CLASSIFICATION_CENTROIDS = {
    "polarized": (80, 5, 15),   # Seiler-style: easy + hard, minimal moderate
    "pyramidal": (80, 15, 5),   # Decreasing pyramid: most easy, some moderate, little hard
    "threshold": (60, 30, 10),  # Heavy on Z3+Z4 / threshold work
    "hiit":      (40, 25, 35),  # HIIT-heavy: substantial Z5+
    "base":      (95, 3, 2),    # Almost all aerobic
}

# A point further than this Euclidean distance from every canonical centroid
# is treated as "edge of band" by `classification_confidence`.
UNIQUE_DISTANCE_THRESHOLD = 35.0

# v1.8.0 — band-centre PI values used by `classification_confidence`.
# Centres approximated from Treff 2019 thresholds: polarized PI > 2.0
# (centre at 2.5), pyramidal 1.0-2.0 (centre 1.5), threshold 0.5-1.0
# (centre 0.75), hiit 0.0-0.5 (centre 0.25).
_BAND_PI_CENTRES = {
    "polarized": 2.5,
    "pyramidal": 1.5,
    "threshold": 0.75,
    "hiit":      0.25,
}
# Approximate half-width of each band — used to map PI-distance to a [0.5, 1.0]
# confidence so non-unique rides always score at least 0.5.
_BAND_PI_HALFWIDTH = {
    "polarized": 0.5,
    "pyramidal": 0.5,
    "threshold": 0.25,
    "hiit":      0.25,
}


def polarization_index(z1z2_pct: float, z3z4_pct: float, z5plus_pct: float) -> float | None:
    """Polarization index = log10((Z1+Z2 + Z5+) / Z3+Z4).

    >0  = polarized (high Z1+Z2 and Z5+, low Z3+Z4)
    ~0  = pyramidal
    <0  = inverted

    Returns None when Z3+Z4 is effectively zero (avoid div-by-zero).
    Inputs are percentages (0-100), not fractions.
    """
    if z3z4_pct < 0.1:
        return None
    try:
        return round(math.log10((z1z2_pct + z5plus_pct) / z3z4_pct), 2)
    except (ValueError, ZeroDivisionError):
        return None


def _closest_centroid(z1z2_pct: float, z3z4_pct: float, z5plus_pct: float) -> tuple[str, float]:
    """Return (label, distance) for the canonical centroid closest to the point."""
    point = (z1z2_pct, z3z4_pct, z5plus_pct)
    best_label, best_dist = None, float("inf")
    for label, centroid in CLASSIFICATION_CENTROIDS.items():
        dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(point, centroid)))
        if dist < best_dist:
            best_dist = dist
            best_label = label
    return best_label, best_dist


def classify_distribution(
    z1z2_pct: float,
    z3z4_pct: float,
    z5plus_pct: float,
    pi: float | None = None,
) -> str:
    """Classify intensity distribution by Treff 2019 PI-band cascade.

    Returns one of: 'polarized', 'pyramidal', 'threshold', 'hiit', 'base', 'unique'.

    Rules (first match wins):
      1. PI > 2.0                                      → polarized
      2. z5+ > 40 AND z1z2 < 20                        → hiit
      3. z3z4 >= 30 AND z5+ <= 15 AND z1z2 <= 50       → threshold
      4. z3z4 >= 35 AND z3z4 > z5+ + 10                → pyramidal
      5. z1z2 >= 70                                    → base
      6. fallthrough                                   → unique

    Threshold is evaluated BEFORE pyramidal so that a high-z3z4 / low-z5+
    / moderate-z1z2 distribution (e.g. 30/60/10) lands on `threshold`
    rather than `pyramidal`. The Treff reference ride (15/49.2/35.8) has
    enough z5+ to skip threshold and is caught by the pyramidal rule.
    """
    if pi is None:
        pi = polarization_index(z1z2_pct, z3z4_pct, z5plus_pct)
    if pi is not None and pi > 2.0:
        return "polarized"
    if z5plus_pct > 40 and z1z2_pct < 20:
        return "hiit"
    if z3z4_pct >= 30 and z5plus_pct <= 15 and z1z2_pct <= 50:
        return "threshold"
    if z3z4_pct >= 35 and z3z4_pct > z5plus_pct + 10:
        return "pyramidal"
    if z1z2_pct >= 70:
        return "base"
    return "unique"


def classification_confidence(
    z1z2_pct: float,
    z3z4_pct: float,
    z5plus_pct: float,
    pi: float | None = None,
) -> float:
    """Confidence in the chosen classification, in [0.0, 1.0].

    PI-distance from band centre maps to [0.5, 1.0] for any non-unique
    label (band-centre → 1.0, band-edge → 0.5). The `base` band has no
    natural PI centre, so it falls back to inverse centroid-distance
    in the same [0.5, 1.0] range. `unique` returns 0.5 — the addendum
    contract is "0.5-1.0 for in-band rides, 0.0-0.5 for unique/edge",
    we sit at the boundary.
    """
    if pi is None:
        pi = polarization_index(z1z2_pct, z3z4_pct, z5plus_pct)
    label = classify_distribution(z1z2_pct, z3z4_pct, z5plus_pct, pi)

    if label == "unique":
        return 0.5

    if label == "polarized" and pi is not None:
        # Addendum formula: min(1.0, (pi - 2.0) / 2.0 + 0.5)
        return round(max(0.5, min(1.0, (pi - 2.0) / 2.0 + 0.5)), 2)

    if label in _BAND_PI_CENTRES and pi is not None:
        centre = _BAND_PI_CENTRES[label]
        half = _BAND_PI_HALFWIDTH[label]
        # 1.0 at centre, 0.5 at edge (band-width = 2*half). Clamp to [0.5, 1.0].
        dist = abs(pi - centre)
        conf = 1.0 - 0.5 * min(1.0, dist / half)
        return round(max(0.5, min(1.0, conf)), 2)

    # `base` (or any band missing PI): inverse centroid-distance, in [0.5, 1.0].
    _l, dist = _closest_centroid(z1z2_pct, z3z4_pct, z5plus_pct)
    conf = 1.0 - 0.5 * min(1.0, dist / UNIQUE_DISTANCE_THRESHOLD)
    return round(max(0.5, min(1.0, conf)), 2)


def compute_polarization_block(time_in_zone: dict | None) -> dict | None:
    """Build the §detail polarization block from a {z1..z7} time-in-zone dict.

    Sums the listed seconds, derives Z1+Z2 / Z3+Z4 / Z5+ percentages, and
    returns ``{z1z2_pct, z3z4_pct, z5plus_pct, polarization_index,
    classification, confidence}``. Returns None if the dict is empty or total
    is 0.
    """
    if not isinstance(time_in_zone, dict):
        return None
    secs = {f"z{i}": int(time_in_zone.get(f"z{i}") or 0) for i in range(1, 8)}
    total = sum(secs.values())
    if total <= 0:
        return None

    def _pct(s: int) -> float:
        return round(100.0 * s / total, 1)

    z1z2 = _pct(secs["z1"] + secs["z2"])
    z3z4 = _pct(secs["z3"] + secs["z4"])
    z5plus = _pct(secs["z5"] + secs["z6"] + secs["z7"])
    pi = polarization_index(z1z2, z3z4, z5plus)
    return {
        "z1z2_pct": z1z2,
        "z3z4_pct": z3z4,
        "z5plus_pct": z5plus,
        "polarization_index": pi,
        "classification": classify_distribution(z1z2, z3z4, z5plus, pi),
        "confidence": classification_confidence(z1z2, z3z4, z5plus, pi),
    }


# ── DFA α1 (v1.0.7, literature-grounded constants v1.8.1) ───────────────────
#
# Detrended Fluctuation Analysis short-term scaling exponent (α1) computed
# over a rider's RR-interval series. Algorithm follows the canonical Peng
# et al. (1995) DFA formulation; physiological application + window / scale
# / threshold constants follow Rogers 2021 (IJSPP) and Gronwald & Hoos 2020
# (Front Physiol).
#
# References:
#   - Peng CK, Havlin S, Stanley HE, Goldberger AL (1995).
#     "Quantification of scaling exponents and crossover phenomena in
#     nonstationary heartbeat time series." Chaos 5(1):82-87.
#     doi:10.1063/1.166141   (DFA method; standard scale range and detrend.)
#   - Rogers B, Giles D, Draper N, Hoos O, Gronwald T (2021).
#     "A New Detection Method Defining the Aerobic Threshold for Endurance
#     Exercise and Training Prescription Based on Fractal Correlation
#     Properties of Heart Rate Variability." Frontiers in Physiology 11:596567.
#     doi:10.3389/fphys.2020.596567   (LT1 / VT1 marker = α1 ≈ 0.75 ± 0.05;
#     short-scale window n ∈ [4, 16]; 120 s sliding window, 30 s step.)
#   - Gronwald T, Hoos O (2020).
#     "Correlation properties of heart rate variability during endurance
#     exercise: a systematic review." Annals of Noninvasive Electrocardiology
#     25(1):e12697. doi:10.1111/anec.12697   (α1 physiological range during
#     exercise: ~0.5 maximal effort to ~1.5 rest; sanity bounds [0.30, 1.60].)
#
# Sliding-window constants:
#   - window_s = 120 s   (Rogers 2021 §2.5; long enough to stabilise α1 fit,
#     short enough to track intensity transitions.)
#   - step_s   = 30 s    (Rogers 2021 §2.5; ≈75% window overlap.)
#
# Per-window fit gates:
#   - R² ≥ 0.95 on the log F(n) vs log n line (rejects windows where the
#     scaling region is not log-linear — typically motion artefact or
#     dropped beats; matches Rogers 2021 §2.5 "good fit" filter, which uses
#     R² > 0.95 for the n=4..16 region.)
#   - α1 ∈ [DFA_SANITY_MIN, DFA_SANITY_MAX] = [0.30, 1.60]
#     (Gronwald & Hoos 2020 review: published α1 in endurance exercise
#     spans ~0.4 [maximal] to ~1.5 [rest]; values outside are signal-quality
#     failures, not physiology.)
#
# Aerobic threshold marker:
#   - DFA_LT1_THRESHOLD = 0.75
#     (Rogers 2021 §3: α1 crossing 0.75 ± 0.05 anchors the first ventilatory
#     threshold / aerobic threshold in trained endurance athletes.)

DFA_SANITY_MIN = 0.30   # Gronwald & Hoos 2020 physiological lower bound.
DFA_SANITY_MAX = 1.60   # Gronwald & Hoos 2020 physiological upper bound.
DFA_LT1_THRESHOLD = 0.75  # Rogers 2021 LT1 anchor.


def _dfa_alpha1_window(rr_window: list[float]) -> float | None:
    """Compute α1 for a single RR-interval window via Peng-style DFA.

    Standard DFA pipeline (Peng et al. 1995):
      1. Integrate: y[k] = Σ (rr[i] - mean(rr)) for i ≤ k.
      2. For each scale n ∈ [4, 16] (Rogers 2021 short-scale α1 region):
         - split y into ⌊N/n⌋ non-overlapping segments of length n,
         - linear least-squares detrend each segment,
         - F(n) = sqrt(mean over segments of mean-squared residuals).
      3. α1 = slope of log F(n) vs log n.
      4. Fit quality gate: R² ≥ 0.95 on the log-log line (Rogers 2021).

    Returns None when:
      - fewer than 16 beats in the window (cannot fit the largest scale n=16),
      - fewer than three valid (n, F(n)) points were obtained,
      - the slope denominator is degenerate, or
      - the log-log fit R² is below 0.95 (poor scaling region).

    The sliding-window caller (compute_dfa_alpha1) applies the
    physiological [DFA_SANITY_MIN, DFA_SANITY_MAX] gate on top of this.
    """
    n_beats = len(rr_window)
    if n_beats < 16:
        return None

    rr_mean = sum(rr_window) / n_beats
    y: list[float] = []
    cumsum = 0.0
    for r in rr_window:
        cumsum += (r - rr_mean)
        y.append(cumsum)
    N = len(y)

    n_values: list[float] = []
    f_values: list[float] = []
    for n in range(4, 17):
        num_segs = N // n
        if num_segs < 2:
            continue
        fluct_sq: list[float] = []
        for s in range(num_segs):
            seg = y[s * n:(s + 1) * n]
            x_mean = (n - 1) / 2.0
            y_mean_seg = sum(seg) / n
            num = sum((i - x_mean) * (seg[i] - y_mean_seg) for i in range(n))
            den = sum((i - x_mean) ** 2 for i in range(n))
            a = num / den if den > 0 else 0.0
            b = y_mean_seg - a * x_mean
            rms_sq = sum((seg[i] - (a * i + b)) ** 2 for i in range(n)) / n
            fluct_sq.append(rms_sq)
        if fluct_sq:
            f_n = math.sqrt(sum(fluct_sq) / len(fluct_sq))
            if f_n > 0:
                n_values.append(math.log(n))
                f_values.append(math.log(f_n))

    if len(n_values) < 3:
        return None

    n_pts = len(n_values)
    x_mean = sum(n_values) / n_pts
    y_mean = sum(f_values) / n_pts
    num = sum((n_values[i] - x_mean) * (f_values[i] - y_mean) for i in range(n_pts))
    den = sum((n_values[i] - x_mean) ** 2 for i in range(n_pts))
    if den <= 0:
        return None
    alpha1 = num / den

    ss_res = sum(
        (f_values[i] - (y_mean + alpha1 * (n_values[i] - x_mean))) ** 2
        for i in range(n_pts)
    )
    ss_tot = sum((f_values[i] - y_mean) ** 2 for i in range(n_pts))
    r_sq = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    if r_sq < 0.95:
        return None

    return alpha1


def compute_dfa_alpha1(
    rr_seconds: list[float],
    window_s: float = 120.0,
    step_s: float = 30.0,
) -> dict:
    """v1.0.7 — sliding-window DFA α1 across an RR-interval series.

    Per Rogers et al. 2021 (Front Physiol 11:596567): a 120-s window sliding
    in 30-s steps provides per-window α1 estimates whose crossing of 0.75
    marks the first ventilatory / aerobic threshold (LT1).

    Args:
        rr_seconds: chronological list of RR-intervals in seconds.
        window_s: window length in seconds (Rogers 2021 §2.5 default = 120).
        step_s:   step between windows in seconds (Rogers 2021 §2.5 = 30).

    Returns:
        ``{avg, series, lt1_minutes, window_s, step_s, n_windows}``

        - ``avg``: mean of all valid per-window α1 values, sanity-gated to
          [DFA_SANITY_MIN, DFA_SANITY_MAX] = [0.30, 1.60] (Gronwald & Hoos
          2020 physiological range). ``None`` if no valid windows OR the
          mean falls outside the sanity range (signal for caller to mark
          ``dfa_alpha1_status='sanity_rejected'``).
        - ``series``: list of ``{min, alpha1}`` per-window values (offset to
          window start in minutes). Includes only valid (sanity-passing) fits.
        - ``lt1_minutes``: minutes spent with α1 < DFA_LT1_THRESHOLD = 0.75
          (Rogers 2021 LT1 marker proxy).
        - ``window_s`` / ``step_s``: echoed for reproducibility.
        - ``n_windows``: count of valid (sanity-passing) windows.
    """
    out_empty = {
        "avg": None,
        "series": [],
        "lt1_minutes": None,
        "window_s": window_s,
        "step_s": step_s,
        "n_windows": 0,
    }

    if not rr_seconds:
        return out_empty
    rr = [float(x) for x in rr_seconds if x and x > 0]
    if len(rr) < 16:
        return out_empty

    # Walk the RR series, advancing by elapsed-time (sum of RR), collecting a
    # window worth of beats per step. Track window-start elapsed time for the
    # series x-axis.
    cum_t: list[float] = []
    t = 0.0
    for r in rr:
        t += r
        cum_t.append(t)

    if cum_t[-1] < window_s:
        return out_empty

    series: list[dict] = []
    valid_alphas: list[float] = []
    lt1_minutes_acc = 0.0

    next_start = 0.0
    end_time = cum_t[-1] - window_s
    while next_start <= end_time + 1e-6:
        # Find indexes of beats whose elapsed time falls in [next_start, next_start + window_s].
        # Linear scan is fine — typical ride is O(10000) beats.
        win_lo = next_start
        win_hi = next_start + window_s
        # Index of first beat with cum_t >= win_lo.
        i_lo = 0
        for i, ct in enumerate(cum_t):
            if ct >= win_lo:
                i_lo = i
                break
        i_hi = len(cum_t)
        for i in range(i_lo, len(cum_t)):
            if cum_t[i] > win_hi:
                i_hi = i
                break

        rr_window = rr[i_lo:i_hi]
        alpha = _dfa_alpha1_window(rr_window)
        if alpha is not None and DFA_SANITY_MIN <= alpha <= DFA_SANITY_MAX:
            alpha_r = round(alpha, 3)
            valid_alphas.append(alpha_r)
            series.append({
                "min": round(next_start / 60.0, 2),
                "alpha1": alpha_r,
            })
            if alpha_r < DFA_LT1_THRESHOLD:
                lt1_minutes_acc += step_s / 60.0

        next_start += step_s

    if not valid_alphas:
        return out_empty

    avg = sum(valid_alphas) / len(valid_alphas)
    avg_r = round(avg, 3)
    if not (DFA_SANITY_MIN <= avg_r <= DFA_SANITY_MAX):
        # Caller is expected to surface this as ``sanity_rejected``.
        return {
            "avg": None,
            "series": series,
            "lt1_minutes": round(lt1_minutes_acc, 2),
            "window_s": window_s,
            "step_s": step_s,
            "n_windows": len(valid_alphas),
        }

    return {
        "avg": avg_r,
        "series": series,
        "lt1_minutes": round(lt1_minutes_acc, 2),
        "window_s": window_s,
        "step_s": step_s,
        "n_windows": len(valid_alphas),
    }


def compute_dfa_alpha1_for_fit(fit_path: Path) -> dict | None:
    """v1.0.7 — chain RR-extraction + sliding-window α1 for a FIT file.

    Returns a dict with these fields ALWAYS set (None values when no RR data
    or the sanity gate rejected the fit). Status field is one of:

      - ``'computed'``        : successful fit, ``dfa_alpha1_avg`` in [0.30, 1.60].
      - ``'no_rr_data'``      : FIT had no HrvMessage records / parse returned [].
      - ``'sanity_rejected'`` : RR present but DFA produced out-of-range α1.

    Returns None only on a hard failure (e.g. fit_path missing / unreadable),
    so callers can distinguish "no DFA available" (None) from "DFA pipeline
    ran, here's the status" (dict).
    """
    try:
        from fit_activity import parse_rr_intervals
        rr = parse_rr_intervals(fit_path)
    except Exception as e:
        _log_dfa.warning(f"compute_dfa_alpha1_for_fit({fit_path}) parse error: {e}")
        return None

    if not rr:
        return {
            "dfa_alpha1_avg": None,
            "dfa_alpha1_series": [],
            "dfa_alpha1_lt1_minutes": None,
            "dfa_alpha1_status": "no_rr_data",
            "rr_intervals_count": 0,
        }

    result = compute_dfa_alpha1(rr)
    if result["avg"] is None:
        # Distinguish: no valid windows at all (n_windows == 0) → no_rr_data
        # (insufficient data even though parse returned beats); vs. valid
        # windows with out-of-range mean → sanity_rejected.
        if result["n_windows"] == 0:
            status = "no_rr_data"
        else:
            status = "sanity_rejected"
        return {
            "dfa_alpha1_avg": None,
            "dfa_alpha1_series": result["series"],
            "dfa_alpha1_lt1_minutes": result["lt1_minutes"],
            "dfa_alpha1_status": status,
            "rr_intervals_count": len(rr),
        }

    return {
        "dfa_alpha1_avg": result["avg"],
        "dfa_alpha1_series": result["series"],
        "dfa_alpha1_lt1_minutes": result["lt1_minutes"],
        "dfa_alpha1_status": "computed",
        "rr_intervals_count": len(rr),
    }


# ── v1.8.2 — planned-vs-actual ride comparison ──────────────────────────────
#
# `compare_plan_to_actual(planned_session, actual_ride)` returns the locked
# 6-field dict per MATCH-B design. Used by `_summarize_ride_for_calendar` to
# attach a `compare` block to each calendar day-row so the UI can render a
# "matched / extended / truncated / different / no_plan" badge.
#
# Decision rules:
#   * `zone_distribution_match` = cosine similarity on the 3-vec
#     [z1z2_pct, z3z4_pct, z5plus_pct]. Cosine (not MSE) is shape-invariant
#     so a longer ride at the same blend still scores high — exactly the
#     "extended" intent.
#   * `tss_delta_pct` threshold = ±25 % per Foster C (1998) "Monitoring
#     training in athletes with reference to overtraining syndrome." Med Sci
#     Sports Exerc 30(7):1164-1168. The same 25 %/1.5× constant gates
#     `_detect_plan_load_alert`'s acute-load spike check.
#   * `duration_delta_min` threshold = ±15 min — small enough that a normal
#     warm-down extension doesn't trigger "extended", large enough that
#     bailing out 20 min early reads as "truncated".
#   * `zone_dist_match_min` = 0.7, `zone_dist_different_max` = 0.5. The
#     0.5-0.7 shoulder is treated as `matched` (avoids false
#     "different_workout" calls on the noisy `_planned_zone_split_minutes`
#     heuristic).
#   * Missing planned → `no_plan`. Missing actual → `missed`. Rest-day +
#     spontaneous ride → `no_plan` (the ride wasn't the plan).

_INTENT_BUCKETS = ("z1z2_pct", "z3z4_pct", "z5plus_pct")


def _planned_zone_pcts(planned_session: dict) -> tuple[float, float, float] | None:
    """Return planned (z1z2_pct, z3z4_pct, z5plus_pct) from a session's
    `zone_dist` block (library row, Z1%..Z6%). Returns None when zone_dist is
    missing or all zero — caller must degrade gracefully rather than fabricate.
    """
    zd = planned_session.get("zone_dist")
    if not isinstance(zd, dict):
        return None
    z12 = float(zd.get("z1") or 0) + float(zd.get("z2") or 0)
    z34 = float(zd.get("z3") or 0) + float(zd.get("z4") or 0)
    z5p = float(zd.get("z5") or 0) + float(zd.get("z6") or 0)
    total = z12 + z34 + z5p
    if total <= 0:
        return None
    return (z12 * 100.0 / total, z34 * 100.0 / total, z5p * 100.0 / total)


def _actual_zone_pcts(actual_ride: dict) -> tuple[float, float, float] | None:
    """Return actual (z1z2_pct, z3z4_pct, z5plus_pct) from either a
    `_summarize_ride_for_calendar` payload (z1z2_min / z3z4_min / z5plus_min)
    or a raw ride with `time_in_zone`. Returns None when no zone data exists.
    """
    # Preferred: calendar-summary shape carries minute fields directly.
    z12 = actual_ride.get("z1z2_min")
    z34 = actual_ride.get("z3z4_min")
    z5p = actual_ride.get("z5plus_min")
    if z12 is not None or z34 is not None or z5p is not None:
        z12f = float(z12 or 0)
        z34f = float(z34 or 0)
        z5pf = float(z5p or 0)
        total = z12f + z34f + z5pf
        if total > 0:
            return (z12f * 100.0 / total, z34f * 100.0 / total, z5pf * 100.0 / total)

    # Fallback: raw time_in_zone (seconds).
    tiz = actual_ride.get("time_in_zone")
    if isinstance(tiz, dict):
        z12s = float(tiz.get("z1") or 0) + float(tiz.get("z2") or 0)
        z34s = float(tiz.get("z3") or 0) + float(tiz.get("z4") or 0)
        z5ps = sum(float(tiz.get(f"z{i}") or 0) for i in (5, 6, 7))
        total = z12s + z34s + z5ps
        if total > 0:
            return (z12s * 100.0 / total, z34s * 100.0 / total, z5ps * 100.0 / total)
    return None


def _cosine3(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """Cosine similarity on a 3-vec. Returns 0.0 when either vector is zero.
    Result is clipped to [0.0, 1.0] (negative cosine is impossible with
    non-negative zone percentages but be defensive)."""
    dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
    na = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
    nb = math.sqrt(b[0] * b[0] + b[1] * b[1] + b[2] * b[2])
    if na <= 0 or nb <= 0:
        return 0.0
    sim = dot / (na * nb)
    if sim < 0.0:
        return 0.0
    if sim > 1.0:
        return 1.0
    return sim


def _actual_duration_min(actual_ride: dict) -> float:
    """Return actual ride duration in minutes from either shape."""
    if "duration_min" in actual_ride and actual_ride.get("duration_min") is not None:
        try:
            return float(actual_ride.get("duration_min") or 0)
        except (TypeError, ValueError):
            pass
    dur_s = actual_ride.get("duration_s")
    if dur_s is None:
        # raw JSON shape: summary.duration_sec or parsed_stats.duration_sec
        summary = actual_ride.get("summary") or {}
        parsed = actual_ride.get("parsed_stats") or {}
        dur_s = summary.get("duration_sec") or parsed.get("duration_sec") or 0
    try:
        return float(dur_s or 0) / 60.0
    except (TypeError, ValueError):
        return 0.0


def compare_plan_to_actual(
    planned_session: dict | None,
    actual_ride: dict | None,
    *,
    tol_tss_pct: float = 0.25,
    tol_duration_min: int = 15,
    zone_dist_match_min: float = 0.7,
    zone_dist_different_max: float = 0.5,
) -> dict:
    """Compare a planned session to an actual ride. Returns a 6-field dict.

    Decision branches (per MATCH-B design):
      ``matched``           — zdm ≥ 0.7 AND |tss_delta| ≤ 25 % AND
                              |duration_delta| ≤ 15 min.
      ``matched_extended``  — zdm ≥ 0.7 AND tss_delta > +25 % AND
                              duration_delta > 0. Foster (1998) flags >25 %
                              over planned as the acute-load spike side.
      ``matched_truncated`` — zdm ≥ 0.7 AND tss_delta < -25 % AND
                              duration_delta < 0.
      ``different_workout`` — zdm < 0.5 (shape disagreement; e.g. planned
                              VO2 but did Z2).
      ``missed``            — planned exists, actual is None.
      ``no_plan``           — no planned session (free ride OR rest day +
                              spontaneous ride OR both None).

    Foster threshold reference: Foster C (1998) "Monitoring training in
    athletes with reference to overtraining syndrome." Med Sci Sports Exerc
    30(7):1164-1168. The 25 % / 1.5× constant matches
    ``app._detect_plan_load_alert``.

    Returns the locked 6-field dict:
      * ``match_status``: one of the 6 branch labels above.
      * ``tss_delta_pct``: (actual - planned) / planned * 100. None when
        planned TSS ≤ 0 or status is no_plan/missed.
      * ``duration_delta_min``: int, actual - planned minutes (0 for
        no_plan/missed).
      * ``zone_distribution_match``: cosine similarity in [0.0, 1.0], or 0.0
        when either side has no zone data (we never fabricate).
      * ``intent_match``: fraction in [0.0, 1.0] of the planned dominant
        zone-bucket that the actual ride delivered (capped at 1.0). 0.0 when
        planned/actual zone data missing.
      * ``reasons``: 2-4 human strings describing the verdict.
    """
    # ---- Branch: nothing planned ----
    if planned_session is None:
        return {
            "match_status": "no_plan",
            "tss_delta_pct": None,
            "duration_delta_min": 0,
            "zone_distribution_match": 0.0,
            "intent_match": 0.0,
            "reasons": ["No planned session for this day."],
        }

    p_stype = (planned_session.get("session_type") or "").lower()

    # ---- Branch: planned rest day (spontaneous ride still = no_plan) ----
    if p_stype == "rest":
        return {
            "match_status": "no_plan",
            "tss_delta_pct": None,
            "duration_delta_min": 0,
            "zone_distribution_match": 0.0,
            "intent_match": 0.0,
            "reasons": ["Rest day was planned."],
        }

    # ---- Branch: planned but missed ----
    if actual_ride is None:
        return {
            "match_status": "missed",
            "tss_delta_pct": None,
            "duration_delta_min": 0,
            "zone_distribution_match": 0.0,
            "intent_match": 0.0,
            "reasons": ["Planned session was not completed."],
        }

    # ---- Numeric deltas ----
    try:
        p_tss = float(planned_session.get("tss") or planned_session.get("tss_estimate") or 0)
    except (TypeError, ValueError):
        p_tss = 0.0
    try:
        a_tss = float(actual_ride.get("tss") or 0)
    except (TypeError, ValueError):
        a_tss = 0.0

    try:
        p_min = float(planned_session.get("duration_min") or 0)
    except (TypeError, ValueError):
        p_min = 0.0
    a_min = _actual_duration_min(actual_ride)

    duration_delta_min = int(round(a_min - p_min))
    tss_delta_pct: float | None = None
    if p_tss > 0:
        tss_delta_pct = round((a_tss - p_tss) / p_tss * 100.0, 1)

    # ---- Zone-distribution cosine similarity ----
    p_vec = _planned_zone_pcts(planned_session)
    a_vec = _actual_zone_pcts(actual_ride)
    if p_vec is None or a_vec is None:
        zdm = 0.0
        zone_data_missing = True
    else:
        zdm = round(_cosine3(p_vec, a_vec), 3)
        zone_data_missing = False

    # ---- Intent match: did the planned dominant bucket get any actual time? ----
    intent_match = 0.0
    if p_vec is not None and a_vec is not None:
        # Pick the dominant planned bucket (largest of the three).
        dom_idx = max(range(3), key=lambda i: p_vec[i])
        p_share = p_vec[dom_idx]
        a_share = a_vec[dom_idx]
        if p_share > 0:
            intent_match = round(min(1.0, a_share / p_share), 3)

    # ---- Decision: zone-data-missing fallback (TSS-only) ----
    reasons: list[str] = []
    if zone_data_missing:
        # Without zone data we can't classify shape; degrade to TSS deltas
        # only. Never emit `different_workout` on missing data — that would
        # be a false positive driven by missing instrumentation.
        if tss_delta_pct is None:
            reasons.append("No TSS or zone data to compare; treating as matched.")
            status = "matched"
        elif tss_delta_pct > tol_tss_pct * 100 and duration_delta_min > 0:
            status = "matched_extended"
            reasons.append(f"Extended: +{int(round(tss_delta_pct))}% TSS, +{duration_delta_min} min.")
            reasons.append("No zone data; classification based on TSS only.")
        elif tss_delta_pct < -tol_tss_pct * 100 and duration_delta_min < 0:
            status = "matched_truncated"
            reasons.append(f"Truncated: {int(round(tss_delta_pct))}% TSS, {duration_delta_min} min.")
            reasons.append("No zone data; classification based on TSS only.")
        else:
            status = "matched"
            reasons.append("On plan (TSS within ±25%); no zone data to verify shape.")
        return {
            "match_status": status,
            "tss_delta_pct": tss_delta_pct,
            "duration_delta_min": duration_delta_min,
            "zone_distribution_match": 0.0,
            "intent_match": intent_match,
            "reasons": reasons[:4],
        }

    # ---- Decision: zone-data-present cascade ----
    if zdm < zone_dist_different_max:
        reasons.append(f"Different workout: zone match {zdm:.2f} (< {zone_dist_different_max:.2f}).")
        if tss_delta_pct is not None:
            reasons.append(f"TSS delta {int(round(tss_delta_pct))}%.")
        return {
            "match_status": "different_workout",
            "tss_delta_pct": tss_delta_pct,
            "duration_delta_min": duration_delta_min,
            "zone_distribution_match": zdm,
            "intent_match": intent_match,
            "reasons": reasons[:4],
        }

    # zdm in shoulder [0.5, 0.7): treat as matched to avoid false positives.
    if zdm < zone_dist_match_min:
        reasons.append(f"On plan: zone shape close enough ({zdm:.2f}).")
        if tss_delta_pct is not None:
            reasons.append(f"TSS delta {int(round(tss_delta_pct))}%.")
        return {
            "match_status": "matched",
            "tss_delta_pct": tss_delta_pct,
            "duration_delta_min": duration_delta_min,
            "zone_distribution_match": zdm,
            "intent_match": intent_match,
            "reasons": reasons[:4],
        }

    # zdm >= 0.7 → check TSS and duration bands.
    if tss_delta_pct is None:
        # Planned TSS missing/zero; can't gate on delta. Call it matched.
        reasons.append(f"On plan: zone shape match {zdm:.2f}; planned TSS unavailable.")
        return {
            "match_status": "matched",
            "tss_delta_pct": None,
            "duration_delta_min": duration_delta_min,
            "zone_distribution_match": zdm,
            "intent_match": intent_match,
            "reasons": reasons[:4],
        }

    within_tss = abs(tss_delta_pct) <= tol_tss_pct * 100
    within_dur = abs(duration_delta_min) <= tol_duration_min
    if within_tss and within_dur:
        reasons.append(f"On plan: zone match {zdm:.2f}.")
        reasons.append(f"TSS delta {int(round(tss_delta_pct))}%, duration delta {duration_delta_min} min.")
        return {
            "match_status": "matched",
            "tss_delta_pct": tss_delta_pct,
            "duration_delta_min": duration_delta_min,
            "zone_distribution_match": zdm,
            "intent_match": intent_match,
            "reasons": reasons[:4],
        }

    if tss_delta_pct > tol_tss_pct * 100 and duration_delta_min > 0:
        reasons.append(f"Extended: +{int(round(tss_delta_pct))}% TSS, +{duration_delta_min} min.")
        reasons.append(f"Zone shape held ({zdm:.2f}).")
        return {
            "match_status": "matched_extended",
            "tss_delta_pct": tss_delta_pct,
            "duration_delta_min": duration_delta_min,
            "zone_distribution_match": zdm,
            "intent_match": intent_match,
            "reasons": reasons[:4],
        }

    if tss_delta_pct < -tol_tss_pct * 100 and duration_delta_min < 0:
        reasons.append(f"Truncated: {int(round(tss_delta_pct))}% TSS, {duration_delta_min} min.")
        reasons.append(f"Zone shape held ({zdm:.2f}).")
        return {
            "match_status": "matched_truncated",
            "tss_delta_pct": tss_delta_pct,
            "duration_delta_min": duration_delta_min,
            "zone_distribution_match": zdm,
            "intent_match": intent_match,
            "reasons": reasons[:4],
        }

    # Mixed signal (e.g. shorter but higher IF, or vice versa): same intent
    # delivered with a different time/load shape — call it matched.
    reasons.append(f"On plan: zone match {zdm:.2f}.")
    reasons.append(f"Mixed delta: TSS {int(round(tss_delta_pct))}%, duration {duration_delta_min} min.")
    return {
        "match_status": "matched",
        "tss_delta_pct": tss_delta_pct,
        "duration_delta_min": duration_delta_min,
        "zone_distribution_match": zdm,
        "intent_match": intent_match,
        "reasons": reasons[:4],
    }
