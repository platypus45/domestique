"""v4.5.5 — Training intensity distribution analytics.

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

Classification (v4.5.5 REFINE-CLASSIFY):
- Treff PI > 2.0 → polarized (research-grounded primary criterion).
- Otherwise: closest canonical centroid wins (Euclidean distance over the
  Z1+Z2 / Z3+Z4 / Z5+ triplet). If the closest centroid is still further
  than ``UNIQUE_DISTANCE_THRESHOLD``, fall back to "unique".
"""
from __future__ import annotations

import math


# Canonical centroids in (Z1+Z2 %, Z3+Z4 %, Z5+ %) space.
CLASSIFICATION_CENTROIDS = {
    "polarized": (80, 5, 15),   # Seiler-style: easy + hard, minimal moderate
    "pyramidal": (80, 15, 5),   # Decreasing pyramid: most easy, some moderate, little hard
    "threshold": (60, 30, 10),  # Heavy on Z3+Z4 / threshold work
    "hiit":      (40, 25, 35),  # HIIT-heavy: substantial Z5+
    "base":      (95, 3, 2),    # Almost all aerobic
}

# A point further than this Euclidean distance from every canonical centroid
# is labelled "unique" — none of the named patterns describes it well.
UNIQUE_DISTANCE_THRESHOLD = 35.0


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
    """Classify intensity distribution by closest canonical centroid.

    Returns one of: 'polarized', 'pyramidal', 'threshold', 'hiit', 'base', 'unique'.

    Treff PI > 2.0 → polarized regardless of centroid distance (the research
    paper's primary criterion). Otherwise the closest of CLASSIFICATION_CENTROIDS
    wins; if the closest distance is greater than UNIQUE_DISTANCE_THRESHOLD,
    the distribution is classified as 'unique'.
    """
    if pi is None:
        pi = polarization_index(z1z2_pct, z3z4_pct, z5plus_pct)
    if pi is not None and pi > 2.0:
        return "polarized"

    label, dist = _closest_centroid(z1z2_pct, z3z4_pct, z5plus_pct)
    if dist > UNIQUE_DISTANCE_THRESHOLD:
        return "unique"
    return label


def classification_confidence(z1z2_pct: float, z3z4_pct: float, z5plus_pct: float) -> float:
    """Confidence in the chosen classification, in [0.0, 1.0].

    Inverse-distance score against the closest centroid: distance 0 → 1.0,
    distance ``UNIQUE_DISTANCE_THRESHOLD`` → 0.0. Beyond that, the result is
    clamped to 0.0 (and the classification itself is "unique").
    """
    _label, dist = _closest_centroid(z1z2_pct, z3z4_pct, z5plus_pct)
    confidence = 1.0 - (dist / UNIQUE_DISTANCE_THRESHOLD)
    if confidence < 0.0:
        return 0.0
    if confidence > 1.0:
        return 1.0
    return round(confidence, 2)


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
        "confidence": classification_confidence(z1z2, z3z4, z5plus),
    }
