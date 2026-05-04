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
    "tempo",
    "sweet_spot",
    "threshold",
    "over_under",
    "vo2max",
    "vo2_short",
    "anaerobic",
    "neuromuscular",
    "ftp_test",
    "mixed",
]

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


# ── Library-wide pass + cache ─────────────────────────────────────────────────


def classify_all(workout_dir: Path) -> dict:
    """Classify every ZWO in workout_dir. Returns dict keyed by basename."""
    results: dict[str, dict] = {}
    files = sorted(workout_dir.glob("*.zwo"))
    for i, zwo in enumerate(files, 1):
        results[zwo.name] = classify_zwo(zwo)
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
        classifications = classify_all(args.workout_dir)
        write_cache(args.output, classifications, args.workout_dir)
        print(f"Wrote {len(classifications)} classifications → {args.output}", file=sys.stderr)
        # Distribution summary
        dist = Counter(c["primary"] for c in classifications.values())
        print("\nPrimary distribution:")
        for k in PRIMARY_TYPES:
            n = dist.get(k, 0)
            print(f"  {k:>14}  {n:>5}  {100*n/max(len(classifications),1):>5.1f}%")
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
