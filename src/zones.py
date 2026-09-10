"""Canonical power & HR zone definitions. Single source of truth across
training_live, ride_storage, app.py, and the planner. Coggan 7-zone power
and Friel 5-zone HR conventions."""
from __future__ import annotations
from typing import NamedTuple

class Zone(NamedTuple):
    low: int        # inclusive lower bound (W for power, bpm for HR)
    high: int       # inclusive upper bound (use 99999 for open-ended top zone)
    name: str       # short canonical name

# Coggan 7-zone power model. Fractions are of FTP.
# Coggan/Allen + ICU standard: Z3 76-90%, Z4 91-105%.
# Z1 Active Recovery: 0-55%
# Z2 Endurance:       56-75%
# Z3 Tempo:           76-90%
# Z4 Threshold:       91-105%
# Z5 VO2max:          106-120%
# Z6 Anaerobic:       121-150%
# Z7 Neuromuscular:   151%+
_POWER_FRACS = [
    (0.00, 0.55, "Z1 Active Recovery"),
    (0.56, 0.75, "Z2 Endurance"),
    (0.76, 0.90, "Z3 Tempo"),
    (0.91, 1.05, "Z4 Threshold"),
    (1.06, 1.20, "Z5 VO2max"),
    (1.21, 1.50, "Z6 Anaerobic"),
    (1.51, 99.99, "Z7 Neuromuscular"),
]

# Friel 5-zone HR model, anchored on LTHR (Z4 lower bound).
# Z1 Recovery:      <81% LTHR
# Z2 Aerobic:       81-89% LTHR
# Z3 Tempo:         90-93% LTHR
# Z4 SubThreshold:  94-99% LTHR
# Z4b Threshold:    100-102% LTHR (merged into Z4 in 5-zone model)
# Z5 VO2max:        103%+ LTHR
# Top is clamped to max_hr.
_HR_FRACS = [
    (0.00, 0.80, "Z1 Recovery"),
    (0.81, 0.89, "Z2 Aerobic"),
    (0.90, 0.93, "Z3 Tempo"),
    (0.94, 1.02, "Z4 Threshold"),
    (1.03, 99.99, "Z5 VO2max"),
]


# ── The three-zone model (Seiler / Treff), and its map onto Coggan ───────────
#
# Every intensity-distribution number in this app -- the phase targets, the
# sampler's budget, the ride-detail polarization card, the on-track score --
# has to mean the same thing. Before this constant existed there were three
# different groupings of the same Coggan zones in the codebase and no two
# agreed; the planner counted threshold work (91-105% FTP) as the hard pole
# while analytics and intervals.icu counted it as the middle.
#
# The three-zone model is anchored on the two physiological thresholds, not on
# zone numbers:
#
#   Z1  below LT1 / VT1
#   Z2  between LT1/VT1 and LT2/VT2      <- the "grey zone" a polarized model
#   Z3  above LT2 / VT2                     deliberately minimises
#
# FTP approximates LT2 (it is the power at which lactate stops reaching a
# steady state), so on the Coggan %FTP scale the seam falls at the top of Z4:
#
#   Coggan Z1+Z2      < 76% FTP     -> three-zone Z1
#   Coggan Z3+Z4      76-105% FTP   -> three-zone Z2   (FTP itself lives here)
#   Coggan Z5+Z6+Z7   >= 106% FTP   -> three-zone Z3
#
# Note the asymmetry this cannot fix: LT1 sits below 75% FTP for many riders,
# so Coggan Z2 slightly over-counts three-zone Z1. That is a property of using
# %FTP as a proxy for a threshold nobody measured, not of this mapping.
THREE_ZONE_FROM_COGGAN: dict[str, tuple[str, ...]] = {
    "z1": ("z1", "z2"),
    "z2": ("z3", "z4"),
    "z3": ("z5", "z6", "z7"),
}


def three_zone(coggan: dict) -> dict[str, float]:
    """Fold a Coggan z1..z7 dict (seconds, minutes, anything additive) into the
    three-zone model. Unknown keys are ignored; missing ones count as zero."""
    return {band: sum(float(coggan.get(k) or 0) for k in keys)
            for band, keys in THREE_ZONE_FROM_COGGAN.items()}


def three_zone_pct(coggan: dict) -> dict[str, float]:
    """``three_zone`` as percentages of the total. All zeros when empty."""
    z = three_zone(coggan)
    total = sum(z.values())
    if total <= 0:
        return {"z1": 0.0, "z2": 0.0, "z3": 0.0}
    return {k: 100.0 * v / total for k, v in z.items()}


def estimated_hr_max(age: int) -> int:
    """Tanaka 2001 formula: 208 - 0.7*age.

    More accurate than the classic 220-age rule across the adult population,
    especially for older athletes where 220-age tends to underestimate HRmax.
    Reference: Tanaka, Monahan & Seals (2001), J Am Coll Cardiol 37(1):153-156.

    Returns a sane default (190) if `age` is not a positive number so callers
    can blindly fall back without a None check.
    """
    if age is None or age <= 0:
        return 190  # sane default
    return int(round(208 - 0.7 * age))


def power_zones(ftp: int) -> list[Zone]:
    """Return list of Zone objects (ascending) derived from FTP in watts.
    Low/high are rounded integers; Z7 high is 99999 (open-ended).

    Boundaries use ``max(prev_high + 1, round(lo_f * ftp))`` so the canonical
    Coggan %FTP fractions are preserved (tests assert e.g. Z4.low == 0.91*FTP).
    Banker's rounding can still leave a 1-W gap between ``round(hi_f * ftp)``
    and ``round(next_lo_f * ftp)`` at certain FTPs — ``power_zone_at`` now
    clamps orphaned boundary values into the neighbouring zone instead of
    silently returning the last zone.
    """
    if ftp <= 0:
        raise ValueError(f"ftp must be positive, got {ftp}")
    out: list[Zone] = []
    prev_high = -1
    for lo_f, hi_f, name in _POWER_FRACS:
        lo = max(prev_high + 1, round(lo_f * ftp))
        hi = 99999 if hi_f >= 99 else round(hi_f * ftp)
        if hi < lo:
            hi = lo
        out.append(Zone(lo, hi, name))
        prev_high = hi
    # Anchor Z1 at 0
    out[0] = Zone(0, out[0].high, out[0].name)
    return out


# Heart-rate-reserve (Karvonen) 5-zone model: target = rest + f x (max - rest).
# Consumer-standard bands; ACSM (Garber 2011, PMID 21694556) classifies
# 40-59 %HRR as moderate and 60-89 %HRR as vigorous, so Z2 sits in
# ACSM-moderate and Z4-Z5 in vigorous. Shipped as an OPTIONAL model: the
# comparative evidence (Wolpern 2015 RCT, PMID 26146564; Meyer 1999; Mann
# 2013) favors threshold-anchored zones when an LTHR is known, and %HRR is
# the evidence-based choice when it is not — docs/SCIENCE.md
# "Heart-rate zones: LTHR vs heart-rate reserve" carries the full verdict.
_HRR_FRACS = [
    (0.00, 0.60, "Z1 Recovery"),
    (0.60, 0.70, "Z2 Aerobic"),
    (0.70, 0.80, "Z3 Tempo"),
    (0.80, 0.90, "Z4 Threshold"),
    (0.90, 99.0, "Z5 VO2max"),
]


def hrr_zones(resting_hr: int, max_hr: int) -> list[Zone]:
    """HR zones from heart-rate reserve (Karvonen). Ascending, gap-free,
    same clamping strategy as hr_zones. Requires BOTH anchors measured —
    callers gate on that; this function only validates."""
    if resting_hr <= 0 or max_hr <= resting_hr:
        raise ValueError(
            f"need 0 < resting_hr < max_hr, got rest={resting_hr} max={max_hr}")
    reserve = max_hr - resting_hr
    out: list[Zone] = []
    prev_high = -1
    for lo_f, hi_f, name in _HRR_FRACS:
        lo = max(prev_high + 1, round(resting_hr + lo_f * reserve))
        hi = max_hr if hi_f >= 99 else round(resting_hr + hi_f * reserve)
        if hi < lo:
            hi = lo
        out.append(Zone(lo, hi, name))
        prev_high = hi
    out[0] = Zone(0, out[0].high, out[0].name)
    return out


def hr_zones(lthr: int, max_hr: int | None = None) -> list[Zone]:
    """Return HR zones (5-zone Friel, ascending). Top zone clamped to max_hr
    if given (else open-ended 99999).

    Same `max(prev_high+1, round(lo_f*lthr))` strategy as ``power_zones`` —
    see its docstring for the rationale. ``hr_zone_at`` handles orphaned
    1-bpm gaps by clamping to the nearest zone.
    """
    if lthr <= 0:
        raise ValueError(f"lthr must be positive, got {lthr}")
    out: list[Zone] = []
    prev_high = -1
    for lo_f, hi_f, name in _HR_FRACS:
        lo = max(prev_high + 1, round(lo_f * lthr))
        if hi_f >= 99:
            hi = max_hr if (max_hr and max_hr > lo) else 99999
        else:
            hi = round(hi_f * lthr)
        if hi < lo:
            hi = lo
        out.append(Zone(lo, hi, name))
        prev_high = hi
    out[0] = Zone(0, out[0].high, out[0].name)
    return out


def _zone_for_value(value: int, zones: list[Zone]) -> int:
    """Resolve a value to its 1-indexed zone, tolerating 1-unit rounding
    gaps between adjacent zones (see `power_zones` docstring). A value
    orphaned in a gap attaches to the higher zone (the one whose nominal
    lower fraction was crossed). Above the top zone returns the last zone.
    """
    for i, z in enumerate(zones, start=1):
        if z.low <= value <= z.high:
            return i
    # Value lies in a rounding gap or above the top zone.
    for i, z in enumerate(zones, start=1):
        if value < z.low:
            return i
    return len(zones)


def power_zone_at(power: int, ftp: int) -> int:
    """1-indexed power zone. 0 if power<=0."""
    if power <= 0:
        return 0
    return _zone_for_value(int(power), power_zones(ftp))


def hr_zone_at(hr: int, lthr: int, max_hr: int | None = None) -> int:
    """1-indexed HR zone. 0 if hr<=0."""
    if hr <= 0:
        return 0
    return _zone_for_value(int(hr), hr_zones(lthr, max_hr))


def zone_distribution(
    samples: list[tuple[int, int]],  # list of (value, duration_sec)
    zones: list[Zone],
) -> list[int]:
    """Sum duration_sec per zone. Returns list parallel to zones.

    Uses ``_zone_for_value`` so values that fall in 1-unit rounding gaps
    between adjacent zones (see `power_zones` docstring) accumulate in the
    same zone that `power_zone_at` / `hr_zone_at` would report.
    """
    out = [0] * len(zones)
    for value, dur in samples:
        if value <= 0 or dur <= 0:
            continue
        idx = _zone_for_value(int(value), zones) - 1
        if 0 <= idx < len(out):
            out[idx] += dur
    return out


__all__ = ["Zone", "power_zones", "hr_zones",
           "power_zone_at", "hr_zone_at", "zone_distribution",
           "estimated_hr_max"]
