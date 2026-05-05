#!/usr/bin/env python3
"""Content-based ZWO workout classifier (v4.1.2 IMPL-CLASSIFIER).

Replaces the filename-prefix heuristic in ``training_planner._classify_protocol``
with a 12-rule cascade applied to the actual power-time profile of each ZWO.
Rules and dose thresholds are derived verbatim from
``/tmp/research_workout_classification.md`` §5/§7 — every threshold is anchored
to a published source (Coggan 2019, Seiler 2013, Billat 1999/2000, Rønnestad
2012, Allen/Coggan/McGregor 2019, Overton/FasCat). See ``CITATIONS`` dict for
the per-rule provenance.

Output schema (per file):
    {
        "file": "<basename>",
        "primary": "<one of PRIMARY_TYPES>",
        "confidence": 0.0..1.0,
        "secondary_flags": {has_threshold_work, has_vo2_work, has_sprints,
                            has_sweet_spot_work, pattern_over_under,
                            pattern_microinterval, polarized_consistent,
                            pyramidal_consistent},
        "features": {duration_s, z1_pct..z7_pct, sweet_spot_pct,
                     hard_segment_count, longest_hard_segment_s,
                     np_fraction, if_fraction, peak_power_fraction},
    }

CLI:
    python3 scripts/classify_library_content.py --file path/to.zwo
    python3 scripts/classify_library_content.py --all
    python3 scripts/classify_library_content.py --all --output workouts/.content_classification.json
    python3 scripts/classify_library_content.py --golden-eval workouts/.golden_set.json
    python3 scripts/classify_library_content.py --compare-filename
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Optional

# ── Constants from research synthesis ────────────────────────────────────────

PRIMARY_TYPES = [
    "recovery",
    "endurance",
    "endurance_intervals",
    "tempo",
    "tempo_intervals",
    "tempo_ladder",
    "sweet_spot",
    "sweet_spot_ladder",
    "threshold",
    "threshold_ladder",
    "over_under",
    "vo2max",
    "vo2_short",
    "vo2_ladder",
    "anaerobic",
    "neuromuscular",
    "ftp_test",
]

# v1.0.4 — locked 16-class canonical taxonomy. See
# /tmp/MASTER_DECISIONS_v104.md §1. ``mixed`` is dropped; structural ladder
# variants and an interval-vs-steady split are introduced. Library JSON now
# emits one of these 16. The legacy ``PRIMARY_TYPES`` list is retained for
# backward-compatibility with the existing planner protocol map and existing
# tests that pre-date the structural rewrite.
CANONICAL_TYPES_V104 = [
    "recovery",
    "endurance",
    "endurance_intervals",
    "tempo",
    "tempo_intervals",
    "tempo_ladder",
    "sweet_spot",
    "sweet_spot_ladder",
    "threshold",
    "threshold_ladder",
    "over_under",
    "vo2max",
    "vo2_short",
    "vo2_ladder",
    "anaerobic",
    "neuromuscular",
    "ftp_test",
]

# Human-readable structure label per canonical class (used by
# ``generate_display_name`` for Layer 3 strings).
_CLASS_LABEL_V104 = {
    "recovery":            "Recovery",
    "endurance":           "Endurance",
    "endurance_intervals": "Endurance + Strides",
    "tempo":               "Tempo",
    "tempo_intervals":     "Tempo Intervals",
    "tempo_ladder":        "Tempo Ladder",
    "sweet_spot":          "Sweet Spot",
    "sweet_spot_ladder":   "Sweet Spot Ladder",
    "threshold":           "Threshold",
    "threshold_ladder":    "Threshold Ladder",
    "over_under":          "Over-Unders",
    "vo2max":              "VO2max",
    "vo2_short":           "VO2 Short",
    "vo2_ladder":          "VO2 Ladder",
    "anaerobic":           "Anaerobic",
    "neuromuscular":       "Neuromuscular",
    "ftp_test":            "FTP Test",
}

# Coggan 7-zone (FTP fractions). Half-open [low, high) to match
# training_planner's existing convention. Allen/Coggan/McGregor 2019.
ZONES_FTP = {
    "z1": (0.00, 0.55),  # Active Recovery
    "z2": (0.55, 0.75),  # Endurance
    "z3": (0.75, 0.90),  # Tempo
    "z4": (0.90, 1.05),  # Threshold (Coggan widens 91-105 → use 90-105)
    "z5": (1.05, 1.20),  # VO2max
    "z6": (1.20, 1.50),  # Anaerobic
    "z7": (1.50, 5.00),  # Neuromuscular
}

# Sweet Spot 88-94% FTP — Frank Overton / FasCat. TrainerRoad ships 88-94%.
SWEET_SPOT_BAND = (0.88, 0.94)

# Per-rule dose thresholds (seconds) — all anchored to literature.
DOSE_RECOVERY_Z1_FRAC = 0.70   # Allen/Coggan: ≥70% Z1 by time
DOSE_RECOVERY_DUR_S = 20 * 60  # Coggan: ≥20 min minimum
DOSE_RECOVERY_BURST_S = 60     # No sustained >75% FTP burst > 60 s
DOSE_RECOVERY_BURST_FRAC = 0.75  # Burst ceiling 75% FTP

DOSE_ENDURANCE_Z2_FRAC = 0.60  # Seiler/San Millán: Z2-dominant
DOSE_ENDURANCE_DUR_S = 45 * 60  # Seiler: ≥45 min to count as Z2 session

DOSE_TEMPO_Z3_S = 20 * 60  # Coggan / TrainerRoad / FasCat: ≥20 min Z3

DOSE_SWEETSPOT_S = 25 * 60  # Overton 2x ~12.5 min minimum
DOSE_SWEETSPOT_FRAC = 0.55  # ≥55% of Z3 time spent in 88-94% band

DOSE_THRESHOLD_Z4_S = 15 * 60  # Allen/Coggan: ≥15 min cumulative

DOSE_OVERUNDER_BAND_S = 18 * 60   # Hunter Allen 3×9min minimum
DOSE_OVERUNDER_TRANSITIONS = 3    # ≥3 above-threshold surges
DOSE_OVERUNDER_BAND = (0.85, 1.10)
# Hunter Allen / Peaks Coaching: under at ~92-100% FTP, over at ≥105% FTP.
# Research §7.1 specifies "transitions between ≥105% and 85-100%". Treat any
# power below 1.00 (and ≥0.70 — see detector) as the "under" half, so 0.95
# under-segments are detected as part of the alternation.
OU_OVER_FRAC = 1.05
OU_UNDER_FRAC = 1.00

DOSE_VO2_Z5_S = 8 * 60  # Laursen & Jenkins 2002 PMID 11772161; Seiler 2013

# VO2 short / Billat / Rønnestad: ≥8 micro-cycles, period ≤90s, on≥1.05, off≤0.75
DOSE_MICRO_MIN_CYCLES = 8
DOSE_MICRO_PERIOD_MAX_S = 90
DOSE_MICRO_ON_FRAC = 0.95   # On-floor (Billat: ≥100% vVO2max ≈ ≥1.05 FTP, but
                            #  filtering ≥0.95 FTP also catches "100% FTP at
                            #  vVO2max" cases). Combined with Z5 dose check,
                            #  we still distinguish from over-under.
DOSE_MICRO_OFF_FRAC = 0.75

DOSE_ANAEROBIC_Z6Z7_S = 3 * 60  # Coggan / FasCat ≥3 min cumulative ≥120% FTP

DOSE_NM_SPRINT_DUR_S = 5    # ≥5s
DOSE_NM_SPRINT_MAX_S = 30   # but ≤30s (longer = anaerobic)
DOSE_NM_SPRINT_FRAC = 1.50  # ≥150% FTP
DOSE_NM_MIN_SPRINTS = 4     # ≥4 sprints to count as a sprint session

# FTP test detection — single sustained high-IF block sandwiched by warmup/cool.
# Coggan 20-min: ≥20 min @ ≥95% FTP (the test itself is all-out, so the floor
# can be relaxed to 92% for slow-paced testers). 18 min as a soft floor accepts
# minor warmup-bleed at the start of the test block. 15 min is too short — that
# falls into threshold territory (Seiler 4×8 or 2×15).
DOSE_FTP_TEST_BLOCK_S = 18 * 60  # ≥18 min sustained — Coggan 20min protocol
DOSE_FTP_TEST_BLOCK_FRAC = 0.92  # ≥92% FTP sustained
DOSE_FTP_TEST_RAMP_STEPS = 5     # Ramp protocols: ≥5 monotonic step-ups
# CTS 2×8 detection uses two ≥8min ≥95% blocks separated by 10min easy
DOSE_FTP_TEST_CTS_BLOCK_S = 8 * 60
DOSE_FTP_TEST_CTS_FRAC = 0.95

# Polarized / pyramidal day-marker thresholds (Stöggl & Sperlich 2014)
POLARIZED_LOW_FRAC = 0.80      # ≥80% Z1+Z2
POLARIZED_MID_FRAC = 0.05      # <5% Z3+Z4 (hardest test)
PYRAMIDAL_LOW_FRAC = 0.65      # majority Z1+Z2 (Stöggl: ~84-95% but be lenient)
PYRAMIDAL_MID_FRAC = 0.05      # ≥5% Z3+Z4 (some)
PYRAMIDAL_HIGH_FRAC = 0.005    # small Z5+Z6+Z7 (1+ minute on a 60-min ride)

# Secondary-flag dose minimums (per research §7.3)
FLAG_THRESHOLD_S = 10 * 60     # has_threshold_work: Z4 ≥10 min
FLAG_VO2_S = 5 * 60            # has_vo2_work: Z5 ≥5 min
FLAG_SPRINT_COUNT = 2          # has_sprints: ≥2 Z7 bursts
FLAG_SWEETSPOT_S = 10 * 60     # has_sweet_spot_work: 88-94% ≥10 min


# Citation table — per rule, source PMID/ISBN/URL. Used by --explain to emit
# rationale alongside the classification.
CITATIONS = {
    "recovery": "Allen/Coggan/McGregor 2019 (ISBN 978-1937715939); TrainerRoad zones doc",
    "endurance": "Seiler & Kjerland 2006 (PMID 16430681); San Millán via Attia/Fast Talk",
    "tempo": "Coggan via Allen 2019; Friel Cyclist's Training Bible 2018",
    "sweet_spot": "Overton/FasCat 'How Much Sweet Spot Training'; TrainerRoad 88-94% canonical",
    "threshold": "Allen/Coggan 2019 'minimum threshold dose ≥15 min'; Seiler 4×8 study (PMID 21812820)",
    "over_under": "Hunter Allen Power Blog (2015); FasCat 'Over Under Intervals'",
    "vo2max": "Laursen & Jenkins 2002 (PMID 11772161); Seiler 4×8 (PMID 21812820); Billat 1999 (PMID 9927024)",
    "vo2_short": "Billat 30-30 (PMID 10638376); Rønnestad 30-15 (PMID 22646668)",
    "anaerobic": "Coggan Z6; Buchheit & Laursen 2013 Pt II (PMID 23832851); FasCat 'Anaerobic Intervals'",
    "neuromuscular": "Coggan Z7; Buchheit & Laursen 2013 Pt II (PMID 23832851)",
    "ftp_test": "Coggan 20-min (Allen/Coggan 2019); Stern Ramp; CTS 8-min (Carmichael & Burke 1994)",
    "mixed": "fallback when no qualifying dose for any single category (Stöggl & Sperlich 2014 pyramidal/polarized framing)",
}


# ── ZWO parsing → 1-Hz power-time array ───────────────────────────────────────


def parse_zwo_full(zwo_path: Path) -> tuple[list[float], list[str], dict, list[dict]]:
    """Like :func:`parse_zwo_to_power_array` but additionally returns a list of
    structured segments — one entry per ZWO ``<workout>`` child element.

    The structured-segment view is what the v1.0.4 cascade uses for the ladder
    detector and peak-zone gate, since reasoning over the original power
    targets (rather than the 1-Hz sampling) lets us recognise rung structure
    and warmup/cooldown framing.

    Each segment dict contains:
        kind: "warmup" | "cooldown" | "ramp" | "steady" | "intervals" |
              "free_ride"
        duration_s: int
        power_low / power_high: float (for ramps/warmup/cooldown)
        power: float (for steady — mid-power)
        on_power / off_power / on_s / off_s / repeat: for intervals
    """
    power_array, tags, meta = parse_zwo_to_power_array(zwo_path)
    tree = ET.parse(zwo_path)
    workout_el = tree.getroot().find("workout")
    segments: list[dict] = []
    if workout_el is None:
        return power_array, tags, meta, segments
    for seg in workout_el:
        tag = seg.tag
        if tag in ("Warmup", "Cooldown", "Ramp"):
            try:
                dur = int(float(seg.get("Duration", 0) or 0))
            except (ValueError, TypeError):
                dur = 0
            if dur <= 0:
                continue
            plo = float(seg.get("PowerLow", 0.5))
            phi = float(seg.get("PowerHigh", 0.7))
            kind = {"Warmup": "warmup", "Cooldown": "cooldown", "Ramp": "ramp"}[tag]
            segments.append({
                "kind": kind, "duration_s": dur,
                "power_low": plo, "power_high": phi,
                "power": (plo + phi) / 2.0,
            })
        elif tag == "SteadyState":
            try:
                dur = int(float(seg.get("Duration", 0) or 0))
            except (ValueError, TypeError):
                dur = 0
            if dur <= 0:
                continue
            p = float(seg.get("Power", 0.65))
            segments.append({
                "kind": "steady", "duration_s": dur, "power": p,
                "power_low": p, "power_high": p,
            })
        elif tag == "IntervalsT":
            try:
                reps = int(seg.get("Repeat", 1))
            except (ValueError, TypeError):
                reps = 1
            try:
                on_s = int(float(seg.get("OnDuration", 0) or 0))
                off_s = int(float(seg.get("OffDuration", 0) or 0))
            except (ValueError, TypeError):
                on_s = off_s = 0
            on_p = float(seg.get("OnPower", 1.0))
            off_p = float(seg.get("OffPower", 0.5))
            segments.append({
                "kind": "intervals", "duration_s": reps * (on_s + off_s),
                "repeat": reps, "on_s": on_s, "off_s": off_s,
                "on_power": on_p, "off_power": off_p,
                "power": on_p,  # peak for ladder detection
                "power_low": min(on_p, off_p), "power_high": max(on_p, off_p),
            })
        elif tag == "FreeRide":
            try:
                dur = int(float(seg.get("Duration", 0) or 0))
            except (ValueError, TypeError):
                dur = 0
            if dur > 0:
                segments.append({
                    "kind": "free_ride", "duration_s": dur,
                    "power": -1.0, "power_low": -1.0, "power_high": -1.0,
                })
    return power_array, tags, meta, segments


def parse_zwo_to_power_array(zwo_path: Path) -> tuple[list[float], list[str], dict]:
    """Parse a ZWO and return (power_per_second[], tags[], meta).

    Power is expressed as a fraction of FTP (1.0 = 100% FTP). FreeRide segments
    are recorded as a sentinel value so they can be excluded from zone counting
    (Free segments are not target-power and shouldn't bias the classifier).

    Linear interpolation is used for Warmup/Cooldown/Ramp segments, which
    matches what trainer hardware actually replays.

    Returns:
        power_array: list[float] — 1-Hz samples in fractional FTP
                     (negative value = FreeRide marker; ignored for zone time)
        tags: list[str] — content of <tags><tag name=…/></tags>
        meta: dict with keys name, description, sport_type
    """
    tree = ET.parse(zwo_path)
    root = tree.getroot()
    name = (root.findtext("name") or zwo_path.stem).strip()
    description = (root.findtext("description") or "").strip()
    sport_type = (root.findtext("sportType") or "bike").strip()

    tags: list[str] = []
    tags_el = root.find("tags")
    if tags_el is not None:
        for tag_el in tags_el.findall("tag"):
            tnm = tag_el.get("name")
            if tnm:
                tags.append(tnm.strip())

    workout_el = root.find("workout")
    if workout_el is None:
        return [], tags, {"name": name, "description": description, "sport_type": sport_type}

    power_array: list[float] = []
    FREE_RIDE_SENTINEL = -1.0

    for seg in workout_el:
        tag = seg.tag
        if tag in ("Warmup", "Cooldown", "Ramp"):
            dur = int(float(seg.get("Duration", 0) or 0))
            plo = float(seg.get("PowerLow", 0.5))
            phi = float(seg.get("PowerHigh", 0.7))
            if dur <= 0:
                continue
            # Linear interpolation 1-Hz
            for t in range(dur):
                frac = t / dur if dur > 1 else 0.0
                p = plo + (phi - plo) * frac
                power_array.append(p)
        elif tag == "SteadyState":
            dur = int(float(seg.get("Duration", 0) or 0))
            p = float(seg.get("Power", 0.65))
            if dur <= 0:
                continue
            power_array.extend([p] * dur)
        elif tag == "IntervalsT":
            reps = int(seg.get("Repeat", 1))
            on_s = int(float(seg.get("OnDuration", 0) or 0))
            off_s = int(float(seg.get("OffDuration", 0) or 0))
            on_p = float(seg.get("OnPower", 1.0))
            off_p = float(seg.get("OffPower", 0.5))
            for _ in range(reps):
                power_array.extend([on_p] * on_s)
                power_array.extend([off_p] * off_s)
        elif tag == "FreeRide":
            dur = int(float(seg.get("Duration", 0) or 0))
            if dur <= 0:
                continue
            # Sentinel — excluded from zone time accounting
            power_array.extend([FREE_RIDE_SENTINEL] * dur)
        # Other tags (MaxEffort etc.) are extremely rare and skipped.

    return power_array, tags, {
        "name": name,
        "description": description,
        "sport_type": sport_type,
    }


# ── Feature extraction ────────────────────────────────────────────────────────


def _zone_for_power(p: float) -> str:
    """Return Coggan zone key (z1..z7) for a power fraction. Half-open [low, high)."""
    if p < ZONES_FTP["z1"][1]:
        return "z1"
    if p < ZONES_FTP["z2"][1]:
        return "z2"
    if p < ZONES_FTP["z3"][1]:
        return "z3"
    if p < ZONES_FTP["z4"][1]:
        return "z4"
    if p < ZONES_FTP["z5"][1]:
        return "z5"
    if p < ZONES_FTP["z6"][1]:
        return "z6"
    return "z7"


def find_contiguous_segments(
    power: list[float], min_frac: float, min_dur_s: int = 1, max_dur_s: int | None = None,
) -> list[tuple[int, int, float]]:
    """Return list of (start_idx, duration_s, mean_power) for contiguous runs
    where p >= min_frac. Filters by duration bounds.
    """
    segments: list[tuple[int, int, float]] = []
    n = len(power)
    i = 0
    while i < n:
        if power[i] >= min_frac and power[i] >= 0:
            start = i
            psum = 0.0
            while i < n and power[i] >= min_frac and power[i] >= 0:
                psum += power[i]
                i += 1
            dur = i - start
            if dur >= min_dur_s and (max_dur_s is None or dur <= max_dur_s):
                segments.append((start, dur, psum / dur))
        else:
            i += 1
    return segments


def compute_np(power: list[float]) -> float:
    """Normalized Power as a fraction of FTP. Coggan: 30-s rolling avg, 4th-power mean."""
    if len(power) < 30:
        return 0.0
    # FreeRide-sentinel filtering: drop negatives.
    p = [x for x in power if x >= 0]
    if len(p) < 30:
        return 0.0
    rolling: list[float] = []
    window = 30
    s = sum(p[:window])
    rolling.append(s / window)
    for i in range(window, len(p)):
        s += p[i] - p[i - window]
        rolling.append(s / window)
    fourth = sum(x ** 4 for x in rolling) / len(rolling)
    return fourth ** 0.25


def detect_over_under_pattern(power: list[float]) -> tuple[bool, int]:
    """Return (is_over_under, transition_count).

    Hunter Allen pattern: alternates ≥1.05 → 0.85-0.92 → ≥1.05 within 90-110%
    band. Looks for at least 3 transitions where power drops from ≥1.05 to
    <0.92 then climbs back to ≥1.05, with each leg lasting at least 30s.
    """
    transitions = 0
    state = None  # "over" or "under"
    leg_start = 0
    n = len(power)
    for i, p in enumerate(power):
        if p < 0:
            continue
        if p >= OU_OVER_FRAC:
            if state == "under" and (i - leg_start) >= 30:
                transitions += 1
                state = "over"
                leg_start = i
            elif state is None:
                state = "over"
                leg_start = i
        elif p < OU_UNDER_FRAC and p >= 0.70:
            if state == "over" and (i - leg_start) >= 30:
                state = "under"
                leg_start = i
            elif state is None:
                state = "under"
                leg_start = i
    is_ou = transitions >= DOSE_OVERUNDER_TRANSITIONS
    return is_ou, transitions


def detect_microinterval_pattern(power: list[float]) -> tuple[bool, int]:
    """Return (is_microinterval, cycle_count).

    Billat/Rønnestad pattern: cycles with period ≤90s, ≥8 cycles,
    on-fraction ≥1.05 / off-fraction ≤0.75. Detects by walking power and
    counting on/off transitions.
    """
    cycles = 0
    state = None  # "on" or "off"
    leg_start = 0
    leg_starts: list[int] = []  # track on-leg starts to verify period
    for i, p in enumerate(power):
        if p < 0:
            continue
        if p >= DOSE_MICRO_ON_FRAC:
            if state == "off":
                # off → on transition closes a cycle
                state = "on"
                leg_starts.append(i)
                leg_start = i
            elif state is None:
                state = "on"
                leg_starts.append(i)
                leg_start = i
        elif p <= DOSE_MICRO_OFF_FRAC:
            if state == "on":
                state = "off"
                leg_start = i
            elif state is None:
                state = "off"
                leg_start = i
        # mid-band power doesn't change state; we want crisp on/off cycles

    if len(leg_starts) < 2:
        return False, 0

    # A "cycle" = consecutive on-onsets within period_max_s of each other
    cycle_count = 0
    for i in range(1, len(leg_starts)):
        gap = leg_starts[i] - leg_starts[i - 1]
        if gap <= DOSE_MICRO_PERIOD_MAX_S:
            cycle_count += 1
    is_micro = cycle_count >= DOSE_MICRO_MIN_CYCLES
    return is_micro, cycle_count


def detect_ftp_test(power: list[float], z6_z7_s: int = 0, sprint_count: int = 0) -> tuple[bool, str]:
    """Return (is_ftp_test, subtype).

    Two patterns:
      * Ramp-style: monotonic power increase across ≥5 consecutive
        plateau steps (each step ≥30s) ending above 110% FTP, with no
        anaerobic spikes preceding the test (i.e. mostly clean ramp from
        warmup to failure).
      * Coggan/CTS-style: a single sustained block ≥8 min at ≥92% FTP
        with LOW variability index (TT character), surrounded by
        warmup + cooldown, with NO anaerobic spikes elsewhere in the ride
        (a real test rides all-out within FTP — no Z6 spikes).

    Disqualifiers:
      * Any meaningful Z6+Z7 work (≥30s cumulative) — tests don't include
        anaerobic intervals.
      * More than one ≥8-min ≥92% block — multi-block intervals are
        VO2max/threshold workouts, not tests.
      * Sprints — tests don't include sprints.
    """
    # Strong disqualifiers: anaerobic work in the ride means it's not a test.
    if z6_z7_s >= 30 or sprint_count >= 1:
        return False, ""

    # Ramp test detection. Step detection: contiguous run of ~identical power ≥30s.
    steps: list[tuple[int, int, float]] = []
    n = len(power)
    i = 0
    while i < n:
        if power[i] < 0:
            i += 1
            continue
        start = i
        cur = power[i]
        while i < n and abs(power[i] - cur) < 0.005 and power[i] >= 0:
            i += 1
        if (i - start) >= 30:
            steps.append((start, i - start, cur))
    if len(steps) >= DOSE_FTP_TEST_RAMP_STEPS:
        run = 1
        peak = steps[0][2]
        for j in range(1, len(steps)):
            if steps[j][2] > steps[j - 1][2] + 0.02:
                run += 1
                peak = max(peak, steps[j][2])
                if run >= DOSE_FTP_TEST_RAMP_STEPS and peak >= 1.10:
                    return True, "ramp"
            else:
                run = 1
                peak = steps[j][2]

    # CTS 2×8 detection — two ≥8min blocks at ≥95% FTP, ~10min recovery.
    cts_blocks = find_contiguous_segments(
        power, DOSE_FTP_TEST_CTS_FRAC, min_dur_s=DOSE_FTP_TEST_CTS_BLOCK_S,
    )
    if len(cts_blocks) == 2:
        s1, d1, m1 = cts_blocks[0]
        s2, d2, m2 = cts_blocks[1]
        gap = s2 - (s1 + d1)
        # Both blocks 8-12 min, gap 6-15 min, similar power (within ±5%)
        if (480 <= d1 <= 720 and 480 <= d2 <= 720
            and 360 <= gap <= 900
            and abs(m1 - m2) <= 0.05):
            # Verify CV (flat TT character) for both blocks
            for s, d, _ in cts_blocks:
                seg = [p for p in power[s:s + d] if p >= 0]
                if not seg:
                    return False, ""
                ms = sum(seg) / len(seg)
                var = sum((p - ms) ** 2 for p in seg) / len(seg)
                if (var ** 0.5) / max(ms, 1e-6) > 0.05:
                    break
            else:
                return True, "cts_2x8"

    # Coggan 20-min sustained-block detection — exactly ONE block ≥18min @≥92%,
    # surrounded by warmup + cooldown. Does NOT match CTS 2×8 above.
    blocks = find_contiguous_segments(power, DOSE_FTP_TEST_BLOCK_FRAC, min_dur_s=DOSE_FTP_TEST_BLOCK_S)
    if not blocks:
        return False, ""
    long_blocks = [b for b in blocks if b[1] >= DOSE_FTP_TEST_BLOCK_S]
    if len(long_blocks) != 1:
        return False, ""
    start, dur, mean_p = long_blocks[0]
    seg_power = [p for p in power[start:start + dur] if p >= 0]
    if not seg_power:
        return False, ""
    mean_seg = sum(seg_power) / len(seg_power)
    var = sum((p - mean_seg) ** 2 for p in seg_power) / len(seg_power)
    cv = (var ** 0.5) / max(mean_seg, 1e-6)
    if cv > 0.05:
        return False, ""
    # Real FTP-test blocks have one constant target. A progressive workout
    # like "10' @90% / 10' @92% / 10' @95% / 10' @98%" can have CV < 5% over
    # 40 min but contains 4 distinct power steps. Count distinct ~1% bins
    # within the block — a true test has 1-2 distinct levels (allowing minor
    # power-ramp wobble for ZWO `power_ramp_allowed=1` flag); progressive
    # threshold blocks have 3+.
    distinct_levels = len(set(round(p, 2) for p in seg_power))
    if distinct_levels > 2:
        return False, ""
    # No later block exceeds this one's mean by >5%
    for s2, d2, m2 in blocks:
        if s2 > start + dur and m2 > mean_seg + 0.05:
            return False, ""
    total = len(power)
    if start > 5 * 60 and (total - (start + dur)) > 3 * 60:
        return True, "sustained"
    return False, ""


def extract_features(power: list[float]) -> dict:
    """Compute zone times, peak metrics, and structural detectors."""
    duration_s = len(power)
    valid = [p for p in power if p >= 0]
    valid_dur = len(valid)

    # Time-in-zone (seconds) — exclude FreeRide
    z_sec: dict[str, int] = {f"z{i}": 0 for i in range(1, 8)}
    for p in power:
        if p < 0:
            continue
        z_sec[_zone_for_power(p)] += 1

    sweet_spot_s = sum(1 for p in power if p >= 0 and SWEET_SPOT_BAND[0] <= p < SWEET_SPOT_BAND[1])
    # Split Z4 into "sweet-spot Z4" (90-94%) vs "true threshold" (95-105%)
    # so Rule 7 (Threshold) doesn't fire on a workout that's entirely in
    # the sweet-spot band but happens to land in Coggan Z4 boundary-wise.
    z4_lower_s = sum(1 for p in power if p >= 0 and 0.90 <= p < 0.95)
    z4_upper_s = z_sec["z4"] - z4_lower_s

    # Hard segments: contiguous p ≥ 0.95, duration ≥ 15s
    hard_segs = find_contiguous_segments(power, 0.95, min_dur_s=15)
    longest_hard_s = max((d for _, d, _ in hard_segs), default=0)

    # Sprint segments: ≥1.50 FTP, 5-30s
    sprint_segs = find_contiguous_segments(
        power, DOSE_NM_SPRINT_FRAC, min_dur_s=DOSE_NM_SPRINT_DUR_S, max_dur_s=DOSE_NM_SPRINT_MAX_S,
    )

    np_frac = compute_np(power)
    if_frac = (sum(p ** 2 for p in valid) / valid_dur) ** 0.5 if valid_dur else 0.0
    peak = max(valid) if valid else 0.0

    is_ou, ou_transitions = detect_over_under_pattern(power)
    is_micro, micro_cycles = detect_microinterval_pattern(power)
    # FTP test detector needs to know about Z6+Z7 + sprint counts to
    # disqualify anaerobic-interval workouts that happen to also contain a
    # sustained ≥92% block.
    is_ftp_test, ftp_subtype = detect_ftp_test(
        power, z6_z7_s=z_sec["z6"] + z_sec["z7"], sprint_count=len(sprint_segs),
    )

    def pct(s: int) -> float:
        return round(100.0 * s / valid_dur, 2) if valid_dur else 0.0

    return {
        "duration_s": duration_s,
        "valid_dur_s": valid_dur,
        "z1_pct": pct(z_sec["z1"]),
        "z2_pct": pct(z_sec["z2"]),
        "z3_pct": pct(z_sec["z3"]),
        "z4_pct": pct(z_sec["z4"]),
        "z5_pct": pct(z_sec["z5"]),
        "z6_pct": pct(z_sec["z6"]),
        "z7_pct": pct(z_sec["z7"]),
        "z_seconds": z_sec,
        "z4_lower_s": z4_lower_s,
        "z4_upper_s": z4_upper_s,
        "sweet_spot_pct": pct(sweet_spot_s),
        "sweet_spot_s": sweet_spot_s,
        "hard_segment_count": len(hard_segs),
        "longest_hard_segment_s": longest_hard_s,
        "sprint_segment_count": len(sprint_segs),
        "np_fraction": round(np_frac, 4),
        "if_fraction": round(if_frac, 4),
        "peak_power_fraction": round(peak, 4),
        "is_over_under": is_ou,
        "ou_transitions": ou_transitions,
        "is_microinterval": is_micro,
        "micro_cycles": micro_cycles,
        "is_ftp_test": is_ftp_test,
        "ftp_test_subtype": ftp_subtype,
    }


# ── 12-rule decision cascade ──────────────────────────────────────────────────


def _confidence_from_dose(actual: float, minimum: float, comfortable: float | None = None) -> float:
    """Map (actual / minimum) to a [0.6, 1.0] confidence score.

    actual = the measured dose (e.g. Z5 seconds, ratio).
    minimum = the dose floor that must be cleared to qualify.
    comfortable = a "well above floor" anchor (e.g. 2× minimum) above which
                  confidence is 1.0. Defaults to 2 × minimum.

    Below minimum returns 0.6 by convention (rule still matched, just at the
    edge — caller should already have gated by minimum). At 2× minimum or
    above returns 1.0.
    """
    if minimum <= 0:
        return 1.0
    comfortable = comfortable if comfortable is not None else 2.0 * minimum
    if actual >= comfortable:
        return 1.0
    if actual <= minimum:
        return 0.6
    # Linear ramp 0.6 → 1.0
    span = comfortable - minimum
    frac = (actual - minimum) / span
    return round(0.6 + 0.4 * max(0.0, min(1.0, frac)), 3)


def classify_features(features: dict, tags: list[str] | None = None) -> tuple[str, float, dict]:
    """Apply the 12-rule cascade. Return (primary_type, confidence, secondary_flags).

    Order strictly matches /tmp/research_workout_classification.md §7.2:
        1. FTP Test
        2. Neuromuscular / Sprint
        3. VO2 Short (microinterval)
        4. Anaerobic Capacity
        5. VO2max (classic long)
        6. Over-Under
        7. Threshold
        8. Sweet Spot
        9. Tempo
        10. Endurance
        11. Recovery
        12. Mixed (fallback)
    """
    z = features["z_seconds"]
    z5_s = z["z5"]
    z4_s = z["z4"]
    z3_s = z["z3"]
    z2_s = z["z2"]
    z1_s = z["z1"]
    z6_s = z["z6"]
    z7_s = z["z7"]
    valid_dur = features["valid_dur_s"]

    secondary = {
        "has_threshold_work": z4_s >= FLAG_THRESHOLD_S,
        "has_vo2_work": z5_s >= FLAG_VO2_S,
        "has_sprints": features["sprint_segment_count"] >= FLAG_SPRINT_COUNT,
        "has_sweet_spot_work": features["sweet_spot_s"] >= FLAG_SWEETSPOT_S,
        "pattern_over_under": features["is_over_under"],
        "pattern_microinterval": features["is_microinterval"],
        # Polarized: ≥80% Z1+Z2 + rest in Z5+; <5% Z3+Z4
        "polarized_consistent": (
            valid_dur > 0
            and (z1_s + z2_s) / valid_dur >= POLARIZED_LOW_FRAC
            and (z3_s + z4_s) / valid_dur < POLARIZED_MID_FRAC
            and (z5_s + z6_s + z7_s) > 0
        ),
        # Pyramidal: majority Z1+Z2 + meaningful Z3+Z4 + small Z5+
        "pyramidal_consistent": (
            valid_dur > 0
            and (z1_s + z2_s) / valid_dur >= PYRAMIDAL_LOW_FRAC
            and (z3_s + z4_s) / valid_dur >= PYRAMIDAL_MID_FRAC
            and (z5_s + z6_s + z7_s) / valid_dur >= PYRAMIDAL_HIGH_FRAC
        ),
    }

    # Tag override: explicit ftp_test tag wins regardless of structure.
    if tags and "ftp_test" in {t.lower() for t in tags}:
        return "ftp_test", 1.0, secondary

    # Rule 1 — FTP Test: dedicated structural detector
    if features["is_ftp_test"]:
        # Confidence high if subtype was confidently detected
        return "ftp_test", 0.9, secondary

    # Rule 2 — Neuromuscular / Sprint
    if features["sprint_segment_count"] >= DOSE_NM_MIN_SPRINTS and z7_s >= 20:
        conf = _confidence_from_dose(features["sprint_segment_count"], DOSE_NM_MIN_SPRINTS, 8)
        return "neuromuscular", conf, secondary

    # Rule 3 — VO2 Short (microinterval) — must precede classic VO2 because
    # microinterval workouts also accumulate Z5 time. Requires the pattern AND
    # ≥8 min cumulative ≥1.05 FTP (Z5+Z6+Z7 — the on-fraction lands above Z5).
    high_intensity_s = z5_s + z6_s + z7_s
    if features["is_microinterval"] and high_intensity_s >= DOSE_VO2_Z5_S:
        conf = _confidence_from_dose(features["micro_cycles"], DOSE_MICRO_MIN_CYCLES, 16)
        return "vo2_short", conf, secondary

    # Rule 4 — Anaerobic Capacity (Z6+Z7 ≥3 min, Z5 < 8 min)
    if (z6_s + z7_s) >= DOSE_ANAEROBIC_Z6Z7_S and z5_s < DOSE_VO2_Z5_S:
        conf = _confidence_from_dose(z6_s + z7_s, DOSE_ANAEROBIC_Z6Z7_S, 6 * 60)
        return "anaerobic", conf, secondary

    # Rule 5 — VO2max (classic long): Z5 ≥ 8 min
    if z5_s >= DOSE_VO2_Z5_S:
        conf = _confidence_from_dose(z5_s, DOSE_VO2_Z5_S, 16 * 60)
        return "vo2max", conf, secondary

    # Rule 6 — Over-Under: alternation pattern + ≥18 min in 85-110% band + ≥3 surges
    band_s = sum(
        1 for p in []  # placeholder — recomputed below
    )
    # Easier: bucket from features. The 85-110% band overlaps Z3/Z4/lower-Z5.
    # Approximation: count z3 + z4 (sweet spot already covered by Z3 in ZONES_FTP).
    band_s = z3_s + z4_s + min(z5_s, 60)  # allow up to 1 min Z5 overlap for surges
    if features["is_over_under"] and band_s >= DOSE_OVERUNDER_BAND_S:
        conf = _confidence_from_dose(features["ou_transitions"], DOSE_OVERUNDER_TRANSITIONS, 8)
        return "over_under", conf, secondary

    # Rule 7 — Threshold: Z4 ≥ 15 min, with the qualifier that the bulk of Z4
    # time must be ≥95% FTP (true threshold). Workouts whose Z4 sits entirely
    # in 90-94% (sweet-spot territory by Overton's band) should not classify
    # as threshold — they belong to Sweet Spot (Rule 8). z4_upper_s = Z4 time
    # at ≥95% FTP, z4_lower_s = Z4 time in 90-94% (= sweet spot upper end).
    z4_upper_s = features.get("z4_upper_s", z4_s)
    if z4_upper_s >= DOSE_THRESHOLD_Z4_S:
        conf = _confidence_from_dose(z4_upper_s, DOSE_THRESHOLD_Z4_S, 30 * 60)
        return "threshold", conf, secondary

    # Rule 8 — Sweet Spot: 88-94% time ≥ 25 min AND ≥55% of (Z3+Z4) time in band.
    # Note: the 88-94% band straddles Z3 (76-90%) and lower Z4 (90-94%) under
    # Coggan's half-open zones, so we compare against Z3+Z4 combined as the
    # "tempo+threshold pool" rather than Z3 alone. Overton/FasCat treat sweet
    # spot as its own band sitting on the Z3/Z4 boundary.
    pool_total = max(z3_s + z4_s, 1)
    ss_ratio = features["sweet_spot_s"] / pool_total
    if features["sweet_spot_s"] >= DOSE_SWEETSPOT_S and ss_ratio >= DOSE_SWEETSPOT_FRAC:
        conf = _confidence_from_dose(features["sweet_spot_s"], DOSE_SWEETSPOT_S, 50 * 60)
        return "sweet_spot", conf, secondary

    # Rule 9 — Tempo: Z3 ≥ 20 min
    if z3_s >= DOSE_TEMPO_Z3_S:
        conf = _confidence_from_dose(z3_s, DOSE_TEMPO_Z3_S, 40 * 60)
        return "tempo", conf, secondary

    # Rule 10 — Endurance: Z2 ≥ 45 min AND duration ≥ 60 min
    if z2_s >= DOSE_ENDURANCE_DUR_S and valid_dur >= 60 * 60:
        conf = _confidence_from_dose(z2_s, DOSE_ENDURANCE_DUR_S, 90 * 60)
        return "endurance", conf, secondary

    # Rule 11 — Active Recovery: Z1 ≥ 70% of duration AND duration ≥ 20 min AND
    #          no sustained >75% FTP burst > 60 s
    if valid_dur >= DOSE_RECOVERY_DUR_S and z1_s / max(valid_dur, 1) >= DOSE_RECOVERY_Z1_FRAC:
        # Check no sustained burst above 75%
        bursts = find_contiguous_segments(
            [p if p >= 0 else 0.0 for p in [features["peak_power_fraction"]]],
            DOSE_RECOVERY_BURST_FRAC,
        )
        # We don't have raw power here; rely on Z3+Z4+Z5+Z6+Z7 = 0 OR longest_hard < 60
        no_long_burst = (z3_s + z4_s + z5_s + z6_s + z7_s) == 0 or features["longest_hard_segment_s"] < DOSE_RECOVERY_BURST_S
        if no_long_burst:
            conf = _confidence_from_dose(z1_s / valid_dur, DOSE_RECOVERY_Z1_FRAC, 0.90)
            return "recovery", conf, secondary

    # Rule 12 — Mixed: fallback
    return "mixed", 0.6, secondary


def classify_zwo(zwo_path: Path) -> dict:
    """Top-level: parse + extract features + classify. Returns the schema dict."""
    try:
        power, tags, meta = parse_zwo_to_power_array(zwo_path)
    except (ET.ParseError, OSError) as e:
        return {
            "file": zwo_path.name,
            "primary": "mixed",
            "confidence": 0.0,
            "error": f"parse: {e}",
            "secondary_flags": {},
            "features": {},
        }
    if not power:
        return {
            "file": zwo_path.name,
            "primary": "mixed",
            "confidence": 0.0,
            "error": "empty workout",
            "secondary_flags": {},
            "features": {},
        }
    features = extract_features(power)
    primary, confidence, secondary = classify_features(features, tags=tags)
    # Strip raw z_seconds from features payload to keep JSON small but preserve
    # numeric fields the schema requires.
    feat_out = {
        "duration_s": features["duration_s"],
        "valid_dur_s": features["valid_dur_s"],
        "z1_pct": features["z1_pct"],
        "z2_pct": features["z2_pct"],
        "z3_pct": features["z3_pct"],
        "z4_pct": features["z4_pct"],
        "z5_pct": features["z5_pct"],
        "z6_pct": features["z6_pct"],
        "z7_pct": features["z7_pct"],
        "z4_lower_s": features.get("z4_lower_s", 0),
        "z4_upper_s": features.get("z4_upper_s", 0),
        "sweet_spot_pct": features["sweet_spot_pct"],
        "hard_segment_count": features["hard_segment_count"],
        "longest_hard_segment_s": features["longest_hard_segment_s"],
        "sprint_segment_count": features["sprint_segment_count"],
        "np_fraction": features["np_fraction"],
        "if_fraction": features["if_fraction"],
        "peak_power_fraction": features["peak_power_fraction"],
        "ou_transitions": features["ou_transitions"],
        "micro_cycles": features["micro_cycles"],
    }
    return {
        "file": zwo_path.name,
        "primary": primary,
        "confidence": round(confidence, 3),
        "secondary_flags": secondary,
        "features": feat_out,
        "tags": tags,
    }


# ── v1.0.4 structural detectors + 16-class cascade ────────────────────────────
#
# Cascade order locked by /tmp/MASTER_DECISIONS_v104.md §2:
#   0. Empty <workout> → flag empty (caller-handled, before invocation)
#   1. FreeRide-only   → flag free_ride (caller-handled, before invocation)
#   2. FTP test
#   3. Neuromuscular / sprint
#   4. Ladder detector → <peak_zone>_ladder
#   5. Peak-zone gate (≥5 min contiguous Z4+, ≥30% of work time)
#   6. Over-Under
#   7. VO2 (vo2_short / vo2max — vo2_ladder branch already taken at step 4)
#   8. Threshold / sweet_spot / tempo_intervals / tempo (existing dose rules)
#   9. Endurance / endurance_intervals
#   10. Recovery
#   11. Zone-dominance fallback (NEVER "mixed")


def _peak_zone_for_power(p: float) -> str:
    """Map a power fraction to the v1.0.4 ladder peak-zone bucket.

    The four ladder buckets are aligned to the §1 taxonomy:
        tempo:       <0.88   (Z3 floor through 87% upper bound — TEMPO ladder)
        sweet_spot:  0.88–0.94  (Overton SS band — SWEET_SPOT ladder)
        threshold:   0.95–1.05  (Coggan LT2 ± 5% — THRESHOLD ladder)
        vo2:         ≥1.06       (VO2max range — VO2 ladder)
    """
    if p < 0.88:
        return "tempo"
    if p < 0.95:
        return "sweet_spot"
    if p < 1.06:
        return "threshold"
    return "vo2"


def detect_ladder(segments: list[dict]) -> dict:
    """Detect ascending/descending steady-state ladders per §2 spec.

    A rung is a SteadyState segment (or an IntervalsT element treated as a
    single high-power on-block at OnPower). The detector walks the segment
    list looking for runs of ≥3 segments that monotonically ascend or descend
    with ≥0.05 (5% FTP) gap between consecutive rungs. After the run, a
    recovery segment ≥45 s with mean power ≤0.50 FTP terminates the set.
    Two such cycles → ladder.
    """
    # Minimum rung duration to count toward the ladder structure. Below this
    # the segment is treated as a transition/recovery (not skipped, not a
    # rung). 30 s matches the audit's "≥30 s sustained" zone definition.
    MIN_RUNG_S = 30
    # Minimum rung duration that's allowed to set the peak-zone classifier.
    # A 60 s blip to 120% FTP doesn't make a workout vo2_ladder if the
    # ≥SUSTAINED_RUNG_S-duration rungs all peak at threshold.
    SUSTAINED_RUNG_S = 90

    # Each rung-tuple is (power, duration_s). We track durations so the
    # peak-zone mapping can use sustained-only rungs.
    rungs_per_set: list[list[tuple[float, int]]] = []
    n = len(segments)
    i = 0
    while i < n and segments[i]["kind"] in ("warmup", "free_ride"):
        i += 1

    while i < n:
        run: list[tuple[float, int]] = []
        j = i
        direction = 0

        while j < n:
            seg = segments[j]
            if seg["kind"] in ("steady", "intervals"):
                p = seg["power"]
                dur = seg.get("duration_s", 0)
                if p < 0.60 or dur < MIN_RUNG_S:
                    break
                if not run:
                    run.append((p, dur))
                    j += 1
                    continue
                gap = p - run[-1][0]
                if abs(gap) < 0.05:
                    j += 1
                    continue
                if direction == 0:
                    direction = 1 if gap > 0 else -1
                    run.append((p, dur))
                    j += 1
                    continue
                if direction == 1 and gap >= 0.05:
                    run.append((p, dur))
                    j += 1
                    continue
                if direction == -1 and gap <= -0.05:
                    run.append((p, dur))
                    j += 1
                    continue
                break
            elif seg["kind"] in ("ramp", "warmup", "cooldown", "free_ride"):
                break
            else:
                break

        if len(run) >= 3:
            rungs_per_set.append(run)
            if j < n:
                seg = segments[j]
                if (seg["kind"] in ("steady", "intervals")
                        and seg["duration_s"] >= 45
                        and seg["power"] < 0.60):
                    j += 1
                elif seg["kind"] in ("ramp", "cooldown") and seg["duration_s"] >= 45:
                    j += 1
            i = j
        else:
            i = max(j, i + 1)

    set_count = len(rungs_per_set)
    if set_count >= 2:
        # Peak power for naming = absolute peak rung. Peak power for
        # classification = highest rung that's at least SUSTAINED_RUNG_S
        # long; falls back to absolute peak if every rung is brief.
        all_powers = [p for rs in rungs_per_set for p, _ in rs]
        all_floors = [min(p for p, _ in rs) for rs in rungs_per_set]
        from collections import Counter as _C
        rung_count = _C(len(rs) for rs in rungs_per_set).most_common(1)[0][0]
        peak_power = max(all_powers)
        sustained = [p for rs in rungs_per_set for p, d in rs if d >= SUSTAINED_RUNG_S]
        classifying_peak = max(sustained) if sustained else peak_power
        min_rung_power = min(all_floors)
        return {
            "is_ladder": True,
            "rung_count": rung_count,
            "set_count": set_count,
            "peak_power": round(peak_power, 4),
            "min_rung_power": round(min_rung_power, 4),
            "peak_zone": _peak_zone_for_power(classifying_peak),
        }
    return {
        "is_ladder": False,
        "rung_count": 0,
        "set_count": 0,
        "peak_power": 0.0,
        "min_rung_power": 0.0,
        "peak_zone": "",
    }


def _peak_band_features(power: list[float], segments: list[dict]) -> dict:
    """Compute peak-zone-gate features required by §2."""
    valid_dur = sum(1 for p in power if p >= 0)

    peak_band = "z1"
    for z in ("z7", "z6", "z5", "z4", "z3", "z2"):
        thresh = ZONES_FTP[z][0]
        run = 0
        found = False
        for p in power:
            if p >= thresh and p >= 0:
                run += 1
                if run >= 30:
                    peak_band = z
                    found = True
                    break
            else:
                run = 0
        if found:
            break

    if peak_band == "z1":
        peak_band_pct = 0.0
    else:
        thresh = ZONES_FTP[peak_band][0]
        time_at_or_above = sum(1 for p in power if p >= thresh and p >= 0)
        peak_band_pct = round(time_at_or_above / max(valid_dur, 1), 4)

    dominant_band = "z1"
    longest = 0
    cur_zone = None
    cur_run = 0
    for p in power:
        if p < 0:
            cur_zone = None
            cur_run = 0
            continue
        z = _zone_for_power(p)
        if z == cur_zone:
            cur_run += 1
        else:
            if cur_run > longest:
                longest = cur_run
                dominant_band = cur_zone or "z1"
            cur_zone = z
            cur_run = 1
    if cur_run > longest:
        longest = cur_run
        dominant_band = cur_zone or "z1"

    hard_count = 0
    longest_hard = 0
    run = 0
    for p in power:
        if p >= 0.85 and p >= 0:
            run += 1
        else:
            if run >= 180:
                hard_count += 1
                if run > longest_hard:
                    longest_hard = run
            run = 0
    if run >= 180:
        hard_count += 1
        if run > longest_hard:
            longest_hard = run

    z4plus_run = 0
    longest_z4plus = 0
    z4_thresh = ZONES_FTP["z4"][0]
    for p in power:
        if p >= z4_thresh and p >= 0:
            z4plus_run += 1
            if z4plus_run > longest_z4plus:
                longest_z4plus = z4plus_run
        else:
            z4plus_run = 0

    work_dur = 0
    warmup_dur = 0
    cooldown_dur = 0
    for seg in segments:
        if seg["kind"] == "warmup":
            warmup_dur += seg["duration_s"]
        elif seg["kind"] == "cooldown":
            cooldown_dur += seg["duration_s"]
        else:
            work_dur += seg["duration_s"]
    if work_dur <= 0:
        work_dur = max(valid_dur - warmup_dur - cooldown_dur, 1)

    z4plus_time = sum(1 for p in power if p >= z4_thresh and p >= 0)
    z4plus_in_work_pct = round(z4plus_time / max(work_dur, 1), 4)

    return {
        "peak_band": peak_band,
        "peak_band_pct": peak_band_pct,
        "dominant_segment_band": dominant_band,
        "hard_segment_count": hard_count,
        "longest_hard_segment_s": longest_hard,
        "longest_z4plus_block_s": longest_z4plus,
        "z4plus_in_work_pct": z4plus_in_work_pct,
        "work_dur_s": work_dur,
    }


def extract_features_v104(power: list[float], segments: list[dict]) -> dict:
    """Compute legacy + v1.0.4 features in one merged dict."""
    legacy = extract_features(power)
    ladder = detect_ladder(segments)
    peak = _peak_band_features(power, segments)
    legacy.update({
        "is_ladder": ladder["is_ladder"],
        "ladder_rung_count": ladder["rung_count"],
        "ladder_set_count": ladder["set_count"],
        "ladder_peak_power": ladder["peak_power"],
        "ladder_min_rung_power": ladder["min_rung_power"],
        "ladder_peak_zone": ladder["peak_zone"],
        "peak_band": peak["peak_band"],
        "peak_band_pct": peak["peak_band_pct"],
        "dominant_segment_band": peak["dominant_segment_band"],
        "hard_segment_count_v104": peak["hard_segment_count"],
        "longest_hard_segment_s_v104": peak["longest_hard_segment_s"],
        "longest_z4plus_block_s": peak["longest_z4plus_block_s"],
        "z4plus_in_work_pct": peak["z4plus_in_work_pct"],
        "work_dur_s": peak["work_dur_s"],
    })
    return legacy


def _audit_reason(old_primary, new_primary, new_entry: dict) -> str:
    """Heuristic reason for an audit transition.

    Looks at the v104 features in ``new_entry`` to give a short
    human-readable rationale. Used for the audit-trail JSON only.
    """
    f = (new_entry.get("features") or {})
    if old_primary == "mixed":
        return f"mixed→{new_primary} via zone-dominance fallback (v104 drops mixed)"
    if new_entry.get("flags"):
        return f"flagged: {','.join(new_entry['flags'])}"
    if f.get("is_ladder") and new_primary and new_primary.endswith("_ladder"):
        return (f"ladder detected: {f.get('ladder_set_count', 0)} sets × "
                f"{f.get('ladder_rung_count', 0)} rungs, "
                f"peak {int(round(f.get('ladder_peak_power', 0) * 100))}% FTP")
    if old_primary != new_primary:
        return (f"reclassified by content: peak={f.get('peak_band')} "
                f"({f.get('peak_band_pct', 0) * 100:.1f}%), "
                f"z4_upper={f.get('z4_upper_s', 0)}s, "
                f"z5={f.get('z5_pct', 0)}%")
    return ""


def _zone_dominance_class(z_seconds: dict) -> str:
    """Mixed-class fallback: pick the dominant-zone class. Never returns
    ``mixed``.

    The naive "max-time zone wins" rule misroutes short workouts dominated
    by Z1 recovery between bursts (e.g. a 2×15s anaerobic session whose
    z1=420 s, z6=30 s) to ``recovery`` — discarding the actual stimulus.
    The refined rule:
        * If the highest-time zone is Z2 or above, use it.
        * If Z1 dominates BUT there is ≥30 s in any zone ≥ Z4, pick the
          highest such zone (the workout's real stimulus).
        * Otherwise default to ``recovery``.
    """
    if not z_seconds:
        return "endurance"
    top = max(z_seconds.items(), key=lambda kv: kv[1])[0]
    label = {
        "z1": "recovery",
        "z2": "endurance",
        "z3": "tempo",
        "z4": "threshold",
        "z5": "vo2max",
        "z6": "anaerobic",
        "z7": "neuromuscular",
    }
    if top != "z1":
        return label[top]
    # Z1-dominated: re-route by hardest non-recovery stimulus if any.
    # Each higher band has a "minimum stimulus" floor — short bursts at z4
    # don't make a workout "threshold" unless cumulative time crosses ~5min,
    # but ≥30 s in z6/z7 is enough to call it anaerobic/NM (those are
    # always-brief by definition).
    floors_s = {"z7": 30, "z6": 60, "z5": 120, "z4": 5 * 60}
    for z in ("z7", "z6", "z5", "z4"):
        if z_seconds.get(z, 0) >= floors_s[z]:
            return label[z]
    # No meaningful hard work — true recovery / Z1+Z2 spin.
    return "recovery"


def classify_v104(features: dict, tags: list[str] | None = None,
                  segments: list[dict] | None = None) -> tuple[str, float, dict]:
    """v1.0.4 cascade — emits one of :data:`CANONICAL_TYPES_V104`."""
    z = features["z_seconds"]
    z1_s, z2_s, z3_s = z["z1"], z["z2"], z["z3"]
    z4_s, z5_s, z6_s, z7_s = z["z4"], z["z5"], z["z6"], z["z7"]
    valid_dur = features["valid_dur_s"]

    secondary = {
        "has_threshold_work": features.get("z4_upper_s", z4_s) >= FLAG_THRESHOLD_S,
        "has_vo2_work": z5_s >= FLAG_VO2_S,
        "has_sprints": features["sprint_segment_count"] >= FLAG_SPRINT_COUNT,
        "has_sweet_spot_work": features["sweet_spot_s"] >= FLAG_SWEETSPOT_S,
        "pattern_over_under": features["is_over_under"],
        "pattern_microinterval": features["is_microinterval"],
        "polarized_consistent": (
            valid_dur > 0
            and (z1_s + z2_s) / valid_dur >= POLARIZED_LOW_FRAC
            and (z3_s + z4_s) / valid_dur < POLARIZED_MID_FRAC
            and (z5_s + z6_s + z7_s) > 0
        ),
        "pyramidal_consistent": (
            valid_dur > 0
            and (z1_s + z2_s) / valid_dur >= PYRAMIDAL_LOW_FRAC
            and (z3_s + z4_s) / valid_dur >= PYRAMIDAL_MID_FRAC
            and (z5_s + z6_s + z7_s) / valid_dur >= PYRAMIDAL_HIGH_FRAC
        ),
        "is_ladder": features.get("is_ladder", False),
    }

    if tags and "ftp_test" in {t.lower() for t in tags}:
        return "ftp_test", 1.0, secondary

    if features["is_ftp_test"]:
        return "ftp_test", 0.9, secondary

    if features["sprint_segment_count"] >= DOSE_NM_MIN_SPRINTS and z7_s >= 20:
        conf = _confidence_from_dose(features["sprint_segment_count"], DOSE_NM_MIN_SPRINTS, 8)
        return "neuromuscular", conf, secondary

    if features.get("is_ladder", False):
        peak_zone = features.get("ladder_peak_zone", "")
        ladder_class = {
            "tempo": "tempo_ladder",
            "sweet_spot": "sweet_spot_ladder",
            "threshold": "threshold_ladder",
            "vo2": "vo2_ladder",
        }.get(peak_zone)
        if ladder_class:
            conf = _confidence_from_dose(features.get("ladder_set_count", 0), 2, 4)
            return ladder_class, conf, secondary

    # Peak-zone gate. Skip when the workout is a microinterval session that
    # hits ≥8 min cumulative Z5+ — those are vo2_short workouts whose Z4+
    # accumulators land >30% by virtue of brief on-cycles, not because the
    # workout is structurally a long Z4 block.
    high_intensity_s = z5_s + z6_s + z7_s
    is_micro_vo2 = (features["is_microinterval"]
                    and high_intensity_s >= DOSE_VO2_Z5_S)
    longest_z4plus = features.get("longest_z4plus_block_s", 0)
    z4plus_pct = features.get("z4plus_in_work_pct", 0.0)
    if not is_micro_vo2 and longest_z4plus >= 5 * 60 and z4plus_pct >= 0.30:
        band = features.get("peak_band", "z4")
        if band == "z4":
            # Distinguish true threshold (≥95% FTP — the LT2 ± 5% zone)
            # from sweet-spot Z4 (90-94%, Overton's band). If most of the
            # Z4 time sits in 90-94% rather than ≥95%, the workout is
            # structurally sweet spot, not threshold.
            z4_upper = features.get("z4_upper_s", 0)
            z4_lower = features.get("z4_lower_s", 0)
            if z4_upper >= z4_lower:
                return "threshold", 0.85, secondary
            return "sweet_spot", 0.85, secondary
        if band == "z5":
            return "vo2max", 0.85, secondary

    band_s = z3_s + z4_s + min(z5_s, 60)
    if features["is_over_under"] and band_s >= DOSE_OVERUNDER_BAND_S:
        conf = _confidence_from_dose(features["ou_transitions"], DOSE_OVERUNDER_TRANSITIONS, 8)
        return "over_under", conf, secondary

    # vo2_short — micro-cycles with ≥8 min cumulative high-intensity time.
    # ``high_intensity_s`` was already computed for the peak-gate guard above.
    if features["is_microinterval"] and high_intensity_s >= DOSE_VO2_Z5_S:
        conf = _confidence_from_dose(features["micro_cycles"], DOSE_MICRO_MIN_CYCLES, 16)
        return "vo2_short", conf, secondary
    if (z6_s + z7_s) >= DOSE_ANAEROBIC_Z6Z7_S and z5_s < DOSE_VO2_Z5_S:
        conf = _confidence_from_dose(z6_s + z7_s, DOSE_ANAEROBIC_Z6Z7_S, 6 * 60)
        return "anaerobic", conf, secondary
    if z5_s >= DOSE_VO2_Z5_S:
        conf = _confidence_from_dose(z5_s, DOSE_VO2_Z5_S, 16 * 60)
        return "vo2max", conf, secondary

    z4_upper_s = features.get("z4_upper_s", z4_s)
    if z4_upper_s >= DOSE_THRESHOLD_Z4_S:
        conf = _confidence_from_dose(z4_upper_s, DOSE_THRESHOLD_Z4_S, 30 * 60)
        return "threshold", conf, secondary

    pool_total = max(z3_s + z4_s, 1)
    ss_ratio = features["sweet_spot_s"] / pool_total
    if features["sweet_spot_s"] >= DOSE_SWEETSPOT_S and ss_ratio >= DOSE_SWEETSPOT_FRAC:
        conf = _confidence_from_dose(features["sweet_spot_s"], DOSE_SWEETSPOT_S, 50 * 60)
        return "sweet_spot", conf, secondary

    if z3_s >= DOSE_TEMPO_Z3_S:
        is_intervals = False
        if segments:
            steady_count = sum(1 for s in segments
                               if s["kind"] == "steady" and 0.75 <= s["power"] < 0.90)
            iv_count = sum(1 for s in segments if s["kind"] == "intervals")
            if iv_count >= 1 or steady_count >= 3:
                is_intervals = True
        conf = _confidence_from_dose(z3_s, DOSE_TEMPO_Z3_S, 40 * 60)
        return ("tempo_intervals" if is_intervals else "tempo"), conf, secondary

    if z2_s >= DOSE_ENDURANCE_DUR_S and valid_dur >= 60 * 60:
        has_strides = (
            features["sprint_segment_count"] >= 2
            or features.get("hard_segment_count_v104", 0) >= 2
        )
        conf = _confidence_from_dose(z2_s, DOSE_ENDURANCE_DUR_S, 90 * 60)
        return ("endurance_intervals" if has_strides else "endurance"), conf, secondary

    if valid_dur >= DOSE_RECOVERY_DUR_S and z1_s / max(valid_dur, 1) >= DOSE_RECOVERY_Z1_FRAC:
        no_long_burst = (z3_s + z4_s + z5_s + z6_s + z7_s) == 0 \
            or features["longest_hard_segment_s"] < DOSE_RECOVERY_BURST_S
        if no_long_burst:
            conf = _confidence_from_dose(z1_s / valid_dur, DOSE_RECOVERY_Z1_FRAC, 0.90)
            return "recovery", conf, secondary

    fallback = _zone_dominance_class(features["z_seconds"])
    return fallback, 0.55, secondary


# ── Display-name (Layer 3) generation ─────────────────────────────────────────


def _round_minutes(seconds: int) -> int:
    """Round seconds to the nearest minute, with floor of 1."""
    if seconds <= 0:
        return 0
    return max(1, (seconds + 30) // 60)


def _detect_interval_signature(segments: list[dict]) -> tuple[int, int, int, float] | None:
    """Return (reps, on_s, off_s, peak_power_pct) for the dominant interval
    pattern, or None."""
    iv_segs = [s for s in segments if s["kind"] == "intervals"]
    if iv_segs:
        iv = max(iv_segs, key=lambda s: s.get("repeat", 0))
        return iv["repeat"], iv["on_s"], iv["off_s"], iv["on_power"]

    pairs: dict[tuple, int] = {}
    body = [s for s in segments if s["kind"] == "steady"]
    i = 0
    while i + 1 < len(body):
        on = body[i]
        off = body[i + 1]
        if on["power"] >= 0.75 and off["power"] < 0.75:
            key = (round(on["power"], 2), round(off["power"], 2),
                   on["duration_s"], off["duration_s"])
            pairs[key] = pairs.get(key, 0) + 1
            i += 2
        else:
            i += 1
    if not pairs:
        return None
    best = max(pairs.items(), key=lambda kv: kv[1])
    (on_p, off_p, on_s, off_s), reps = best
    if reps < 2:
        return None
    return reps, on_s, off_s, on_p


def generate_display_name(primary: str, features: dict, segments: list[dict],
                          meta: dict | None = None) -> str:
    """Layer-3 display name per §3 schema."""
    duration_min = _round_minutes(features.get("duration_s", 0))
    label = _CLASS_LABEL_V104.get(primary, primary.replace("_", " ").title())

    if primary == "ftp_test":
        return f"FTP Test {duration_min}min"

    if features.get("is_ladder", False):
        start = int(round(features.get("ladder_min_rung_power", 0.85) * 100))
        peak = int(round(features.get("ladder_peak_power", 0.95) * 100))
        sets = features.get("ladder_set_count", 2)
        return f"{label} {duration_min}min — {start}→{peak}% × {sets}"

    sig = _detect_interval_signature(segments)
    if sig is not None and primary not in ("recovery", "endurance"):
        reps, on_s, off_s, on_p = sig
        peak_pct = int(round(on_p * 100))

        def fmt(secs: int) -> str:
            if secs >= 60 and secs % 60 == 0:
                return f"{secs // 60}min"
            return f"{secs}s"
        return f"{label} {duration_min}min — {reps}×{fmt(on_s)}/{fmt(off_s)} @ {peak_pct}%"

    band = features.get("dominant_segment_band") or features.get("peak_band", "z2")
    return f"{label} {duration_min}min — {band.upper()}"


def classify_zwo_v104(zwo_path: Path) -> dict:
    """Parse + extract v104 features + classify. Returns the new schema dict."""
    try:
        power, tags, meta, segments = parse_zwo_full(zwo_path)
    except (ET.ParseError, OSError) as e:
        return {
            "file": zwo_path.name,
            "primary": "endurance",
            "display_name": f"Endurance {zwo_path.stem}",
            "confidence": 0.0,
            "error": f"parse: {e}",
            "secondary_flags": {},
            "features": {},
            "tags": [],
            "flags": ["parse_error"],
        }
    if not power:
        return {
            "file": zwo_path.name,
            "primary": None,
            "display_name": "",
            "confidence": 0.0,
            "secondary_flags": {},
            "features": {"duration_s": 0, "valid_dur_s": 0},
            "tags": tags,
            "flags": ["empty"],
        }
    valid = [p for p in power if p >= 0]
    if not valid or all(s["kind"] in ("free_ride", "warmup", "cooldown") for s in segments):
        duration_min = _round_minutes(len(power))
        return {
            "file": zwo_path.name,
            "primary": None,
            "display_name": f"Free Ride {duration_min}min",
            "confidence": 0.0,
            "secondary_flags": {},
            "features": {"duration_s": len(power), "valid_dur_s": len(valid)},
            "tags": tags,
            "flags": ["free_ride"],
        }

    features = extract_features_v104(power, segments)
    primary, confidence, secondary = classify_v104(features, tags=tags, segments=segments)
    display_name = generate_display_name(primary, features, segments, meta=meta)

    feat_out = {
        "duration_s": features["duration_s"],
        "valid_dur_s": features["valid_dur_s"],
        "z1_pct": features["z1_pct"],
        "z2_pct": features["z2_pct"],
        "z3_pct": features["z3_pct"],
        "z4_pct": features["z4_pct"],
        "z5_pct": features["z5_pct"],
        "z6_pct": features["z6_pct"],
        "z7_pct": features["z7_pct"],
        "z4_lower_s": features.get("z4_lower_s", 0),
        "z4_upper_s": features.get("z4_upper_s", 0),
        "sweet_spot_pct": features["sweet_spot_pct"],
        "hard_segment_count": features["hard_segment_count"],
        "longest_hard_segment_s": features["longest_hard_segment_s"],
        "sprint_segment_count": features["sprint_segment_count"],
        "np_fraction": features["np_fraction"],
        "if_fraction": features["if_fraction"],
        "peak_power_fraction": features["peak_power_fraction"],
        "ou_transitions": features["ou_transitions"],
        "micro_cycles": features["micro_cycles"],
        "is_ladder": features.get("is_ladder", False),
        "ladder_rung_count": features.get("ladder_rung_count", 0),
        "ladder_set_count": features.get("ladder_set_count", 0),
        "ladder_peak_power": features.get("ladder_peak_power", 0.0),
        "ladder_peak_zone": features.get("ladder_peak_zone", ""),
        "peak_band": features.get("peak_band", ""),
        "peak_band_pct": features.get("peak_band_pct", 0.0),
        "dominant_segment_band": features.get("dominant_segment_band", ""),
        "longest_z4plus_block_s": features.get("longest_z4plus_block_s", 0),
        "z4plus_in_work_pct": features.get("z4plus_in_work_pct", 0.0),
        "work_dur_s": features.get("work_dur_s", 0),
    }

    return {
        "file": zwo_path.name,
        "primary": primary,
        "display_name": display_name,
        "confidence": round(confidence, 3),
        "secondary_flags": secondary,
        "features": feat_out,
        "tags": tags,
    }


# ── Library-wide pass + cache ─────────────────────────────────────────────────


def classify_all(workout_dir: Path, *, use_v104: bool = True) -> dict:
    """Classify every ZWO in workout_dir. Returns dict keyed by basename.

    With ``use_v104=True`` (default) emits the v1.0.4 16-class schema.
    """
    classifier = classify_zwo_v104 if use_v104 else classify_zwo
    results: dict[str, dict] = {}
    files = sorted(workout_dir.glob("*.zwo"))
    for i, zwo in enumerate(files, 1):
        results[zwo.name] = classifier(zwo)
        if i % 500 == 0:
            print(f"  …classified {i}/{len(files)}", file=sys.stderr)
    return results


def compute_workouts_dir_hash(workout_dir: Path) -> str:
    """SHA-256 over (filename, mtime) tuples — cheap invalidation signal."""
    h = hashlib.sha256()
    for p in sorted(workout_dir.glob("*.zwo")):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0
        h.update(f"{p.name}:{mtime}\n".encode())
    return h.hexdigest()


def write_cache(cache_path: Path, classifications: dict, workout_dir: Path) -> None:
    """Write the classification cache + library-state hash."""
    payload = {
        "version": 1,
        "workouts_dir_hash": compute_workouts_dir_hash(workout_dir),
        "count": len(classifications),
        "classifications": classifications,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def load_cache(cache_path: Path) -> dict | None:
    if not cache_path.exists():
        return None
    try:
        with cache_path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ── Filename-based classifier (mirror of training_planner._classify_protocol) ──


def filename_classify(filename: str) -> str:
    """Mirror of training_planner._classify_protocol's filename prefix path,
    expressed in the same primary-type vocabulary used by this module's cascade.
    Used for the --compare-filename diff report only.
    """
    fname = filename.lower()
    if fname.startswith("vo2max_"):
        return "vo2max"
    if fname.startswith("vo2_"):
        return "vo2max"
    if fname.startswith("threshold_") or fname.startswith("supra_threshold"):
        return "threshold"
    if fname.startswith("sweetspot_") or fname.startswith("sweet_spot_"):
        return "sweet_spot"
    if fname.startswith("over_under_"):
        return "over_under"
    if fname.startswith("sprints_"):
        return "neuromuscular"
    if fname.startswith("anaerobic_"):
        return "anaerobic"
    if fname.startswith("pyramid_"):
        return "mixed"
    if fname.startswith("ftp_test_"):
        return "ftp_test"
    if fname.startswith("tempo_"):
        return "tempo"
    if fname.startswith("recovery_"):
        return "recovery"
    if fname.startswith("z2_") or fname.startswith("endurance_"):
        return "endurance"
    if fname.startswith("ramp_"):
        return "threshold"
    if fname.startswith("warmup_"):
        return "recovery"
    if fname.startswith("intervals_"):
        return "mixed"
    return "mixed"


# ── Golden-set evaluation ─────────────────────────────────────────────────────


def evaluate_golden(golden_path: Path, classifications: dict) -> tuple[float, dict, list[dict]]:
    """Compute primary-type accuracy on a golden set.

    Returns (accuracy, confusion_counts, mismatches).
    """
    with golden_path.open() as f:
        golden = json.load(f)
    if not isinstance(golden, list):
        raise ValueError("golden set must be a JSON array")

    n_correct = 0
    confusion: dict[tuple[str, str], int] = {}
    mismatches: list[dict] = []
    for entry in golden:
        fname = entry["file"]
        expected = entry["expected_primary"]
        got = classifications.get(fname, {}).get("primary", "missing")
        confusion[(expected, got)] = confusion.get((expected, got), 0) + 1
        if expected == got:
            n_correct += 1
        else:
            mismatches.append({
                "file": fname,
                "expected": expected,
                "got": got,
                "rationale": entry.get("rationale", ""),
                "features": classifications.get(fname, {}).get("features", {}),
            })
    accuracy = n_correct / len(golden) if golden else 0.0
    return accuracy, confusion, mismatches


def print_confusion_matrix(confusion: dict, total: int) -> None:
    types = sorted(set([k[0] for k in confusion] + [k[1] for k in confusion]))
    print("\nConfusion matrix (rows=expected, cols=got):")
    header = "{:>14}".format("") + "".join("{:>14}".format(t) for t in types)
    print(header)
    for r in types:
        row = "{:>14}".format(r)
        for c in types:
            row += "{:>14}".format(confusion.get((r, c), 0))
        print(row)
    print(f"\nTotal: {total}")


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
    here = Path(__file__).resolve().parent.parent
    default_workout_dir = here / "workouts"
    default_cache = default_workout_dir / ".content_classification.json"
    default_golden = default_workout_dir / ".golden_set.json"

    ap = argparse.ArgumentParser(description="Content-based ZWO workout classifier")
    ap.add_argument("--file", type=Path, help="Classify a single ZWO file")
    ap.add_argument("--all", action="store_true", help="Classify every ZWO in --workout-dir")
    ap.add_argument("--workout-dir", type=Path, default=default_workout_dir)
    ap.add_argument("--output", type=Path, default=default_cache,
                    help="Cache path for --all (default: workouts/.content_classification.json)")
    ap.add_argument("--golden-eval", type=Path,
                    help="Evaluate accuracy on a golden-set JSON")
    ap.add_argument("--compare-filename", action="store_true",
                    help="Compare content classification to filename-prefix classification")
    ap.add_argument("--explain", action="store_true",
                    help="With --file, also print citation/rationale")
    args = ap.parse_args()

    if args.file:
        result = classify_zwo(args.file)
        print(json.dumps(result, indent=2))
        if args.explain:
            cite = CITATIONS.get(result["primary"], "")
            print(f"\nCitation: {cite}")
        return 0

    if args.all:
        print(f"Classifying all ZWO files in {args.workout_dir} …", file=sys.stderr)
        # v1.0.4: load prior cache (for audit-trail "was → now" diff) BEFORE
        # we overwrite it.
        prior = load_cache(args.output) or {}
        prior_classifications = prior.get("classifications", {})
        classifications = classify_all(args.workout_dir, use_v104=True)

        # Merge any user-curated metadata from the prior cache that the new
        # classifier doesn't itself produce. Specifically tags added by
        # post-classification scripts (e.g. is_ronnestad from
        # reclassify_mixed_v461.py) — these aren't in the ZWO <tags>
        # element so the parser doesn't see them.
        for fname, new_entry in classifications.items():
            old = prior_classifications.get(fname, {})
            old_tags = old.get("tags") or []
            new_tags = list(new_entry.get("tags") or [])
            for t in old_tags:
                if t not in new_tags:
                    new_tags.append(t)
            new_entry["tags"] = new_tags

        write_cache(args.output, classifications, args.workout_dir)
        print(f"Wrote {len(classifications)} classifications → {args.output}", file=sys.stderr)

        # Audit trail — write classification_audit_v104.json with class
        # transitions for every file whose primary changed.
        audit_path = args.output.parent / ".classification_audit_v104.json"
        transitions: list[dict] = []
        for fname, new_entry in classifications.items():
            old = prior_classifications.get(fname, {})
            old_primary = old.get("primary")
            new_primary = new_entry.get("primary")
            if old_primary != new_primary:
                transitions.append({
                    "file": fname,
                    "old_primary": old_primary,
                    "new_primary": new_primary,
                    "reason": _audit_reason(old_primary, new_primary, new_entry),
                })
        from collections import Counter as _AC
        with audit_path.open("w", encoding="utf-8") as f:
            json.dump({
                "schema_version": "v1.0.4",
                "transitions": transitions,
                "summary": {
                    "total_files": len(classifications),
                    "transitioned": len(transitions),
                    "old_class_counts": dict(_AC(
                        prior_classifications.get(fn, {}).get("primary")
                        for fn in classifications)),
                    "new_class_counts": dict(_AC(
                        c.get("primary") for c in classifications.values())),
                },
            }, f, indent=2)
        print(f"Wrote audit trail ({len(transitions)} transitions) → {audit_path}",
              file=sys.stderr)

        dist = Counter(c.get("primary") for c in classifications.values())
        print("\nPrimary distribution:")
        for k in CANONICAL_TYPES_V104:
            n = dist.get(k, 0)
            print(f"  {k:>22}  {n:>5}  {100*n/max(len(classifications),1):>5.1f}%")
        empty = sum(1 for c in classifications.values()
                    if "empty" in (c.get("flags") or []))
        free = sum(1 for c in classifications.values()
                   if "free_ride" in (c.get("flags") or []))
        if empty:
            print(f"  {'(empty)':>22}  {empty:>5}  flagged, not classified")
        if free:
            print(f"  {'(free_ride)':>22}  {free:>5}  flagged, not classified")
        return 0

    if args.golden_eval:
        cache = load_cache(args.output)
        if cache is None:
            print(f"No cache at {args.output}; run --all first.", file=sys.stderr)
            return 1
        classifications = cache["classifications"]
        accuracy, confusion, mismatches = evaluate_golden(args.golden_eval, classifications)
        total = sum(confusion.values())
        print(f"\nGolden-set accuracy: {100*accuracy:.1f}%  ({total - len(mismatches)}/{total})")
        print_confusion_matrix(confusion, total)
        if mismatches:
            print(f"\nMismatches ({len(mismatches)}):")
            for m in mismatches:
                f = m["features"]
                print(f"  {m['file']:55} expected={m['expected']:>14} got={m['got']:>14}")
                print(f"    rationale: {m['rationale']}")
                if f:
                    print(f"    feats: z3={f.get('z3_pct',0)}% z4={f.get('z4_pct',0)}% "
                          f"z5={f.get('z5_pct',0)}% sweet={f.get('sweet_spot_pct',0)}% "
                          f"micro_cycles={f.get('micro_cycles',0)} np={f.get('np_fraction',0)}")
        return 0 if accuracy >= 0.90 else 2

    if args.compare_filename:
        cache = load_cache(args.output)
        if cache is None:
            print(f"No cache at {args.output}; run --all first.", file=sys.stderr)
            return 1
        classifications = cache["classifications"]
        agree = 0
        disagree: list[tuple[str, str, str]] = []
        per_cat_mismatch: Counter = Counter()
        for fname, c in classifications.items():
            content = c["primary"]
            fnm = filename_classify(fname)
            if content == fnm:
                agree += 1
            else:
                disagree.append((fname, fnm, content))
                per_cat_mismatch[(fnm, content)] += 1
        total = len(classifications)
        print(f"Agreement: {100*agree/total:.1f}% ({agree}/{total})")
        print(f"Disagreements: {len(disagree)}")
        print("\nTop mismatched (filename → content) categories:")
        for (a, b), n in per_cat_mismatch.most_common(15):
            print(f"  {a:>14} → {b:<14} : {n}")
        print("\nFirst 20 disagreement examples:")
        for fname, a, b in disagree[:20]:
            print(f"  {fname:50} fname={a:>14}  content={b:<14}")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
