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


# ── DFA α1 (v1.0.7) ──────────────────────────────────────────────────────────
#
# Detrended Fluctuation Analysis short-term scaling exponent (α1) over a
# rider's RR-interval series, computed in a sliding 120-s / 30-s-step window
# per Rogers 2021 (PMID 34547011). Ported from the dormant in-ride
# implementation at training_live.py:823-942 — same maths, sliding-window
# wrapper, no nolds dependency.
#
# Sanity gate: any fit producing α1 outside [0.30, 1.60] is dropped (Rogers
# physiological range; values outside indicate dropped beats / data quality).

DFA_SANITY_MIN = 0.30
DFA_SANITY_MAX = 1.60
DFA_LT1_THRESHOLD = 0.75


def _dfa_alpha1_window(rr_window: list[float]) -> float | None:
    """Compute α1 for a single RR-interval window (no sanity / R² gate).

    Algorithm matches training_live._compute_dfa_alpha1 maths:
      1. Cumulative sum of mean-removed RR.
      2. Per-segment linear-detrended RMS for n in [4, 16].
      3. Log-log slope across n vs F(n).
      4. R² ≥ 0.95 required for the fit.

    Returns None when fewer than three valid (n, F(n)) points fit, the slope
    is degenerate, or R² is below 0.95.
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

    Args:
        rr_seconds: chronological list of RR-intervals in seconds.
        window_s: window length in seconds (Rogers 2021 default = 120 s).
        step_s: step between windows in seconds (default = 30 s).

    Returns:
        ``{avg, series, lt1_minutes, window_s, step_s, n_windows}``

        - ``avg``: mean of all valid per-window α1 values, sanity-gated to
          [0.30, 1.60]. ``None`` if no valid windows OR the mean falls
          outside the sanity range (signal for caller to mark
          ``dfa_alpha1_status='sanity_rejected'``).
        - ``series``: list of ``{min, alpha1}`` per-window values (offset to
          window start in minutes). Includes only valid (sanity-passing) fits.
        - ``lt1_minutes``: minutes spent with α1 < 0.75 (LT1 marker proxy).
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
