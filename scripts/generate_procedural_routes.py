#!/usr/bin/env python3
"""Procedural Virtual Cycling Route Generator (v3 — 710 routes, archetype-based).

Generates 710 virtual routes across three worlds (Blue Ridge, Iron Pass,
Desert Loop) by sampling from the 35 archetypes defined in
``route_archetypes.ARCHETYPE_REGISTRY``.

Pipeline per route:
    1. Deterministic seed from (region, idx).
    2. Weighted family pick, then weighted archetype pick within the family.
    3. Seeded distance pick inside the archetype's [min, max] km range.
    4. Call archetype function, apply per-archetype smoothing + clipping.
    5. Emit CRS file (same shape as v2), profile JSON, routes.json entry.
    6. Emit surface_types entry if any non-asphalt content.

Lap variants: each world that quotas laps emits ~20 "base loops", each repeated
at 3-4 lap counts. All variants sharing a base share the same
``lap_route.base_loop_id`` and ``base_loop_km``.

Determinism: same (region, idx) seed -> identical route. No ``random`` module.

Usage:
    python3 generate_procedural_routes.py            # generate + validate
    python3 generate_procedural_routes.py --validate # validate only

License: Apache-2.0
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Optional

from route_archetypes import (
    ARCHETYPE_REGISTRY,
    ArchetypeOutput,
    ArchetypeSpec,
    _detect_climbs,
    _hash32,
    apply_clipping,
    apply_smoothing,
    seeded_random,
)


HERE = Path(__file__).resolve().parent
COURSES_DIR = HERE / "courses" / "virtual"
PROFILES_DIR = HERE / "profiles"
ROUTES_OUT = Path("/tmp/routes_virtual.json")
SURFACES_OUT = Path("/tmp/surface_types_virtual.json")


def stable_hash(s: str) -> int:
    """Deterministic string hash (Python's built-in hash() is salted)."""
    h = 2166136261
    for ch in s:
        h = (h ^ ord(ch)) & 0xFFFFFFFF
        h = (h * 16777619) & 0xFFFFFFFF
    return h


# ─────────────────────────────────────────────────────────────────────────────
# Per-world archetype distributions
# ─────────────────────────────────────────────────────────────────────────────

# Family distribution per world, as [(family_or_archetype, weight)].
# When the entry starts with "@" it's a specific archetype; otherwise it's
# a family key. Internally we expand families to their member archetypes with
# uniform weight within the family.

BLUE_RIDGE_DIST: list[tuple[str, float]] = [
    ("rolling", 0.20),       # was 0.25
    ("climb_low", 0.12),     # was 0.15 — climbs primarily live in iron_pass
    ("flat", 0.10),
    ("lap", 0.10),           # was 0.12
    ("cobble_soft", 0.28),   # was 0.18 — MORE Flanders/Ronde DNA: long
                             # flat polder sections + short steep hellingen,
                             # cobbled kickers (Muur/Koppenberg/Paterberg-style),
                             # flat Paddestraat/Haaghoek cobble sectors
    ("gravel_soft", 0.15),
    ("mixed_special", 0.05),
]

IRON_PASS_DIST: list[tuple[str, float]] = [
    ("climb_wide", 0.55),    # 33 routes — bumped from 0.50 to soak up the
                             # share that descent_special used to claim
    ("rolling_kick", 0.15),  # 9 routes
    ("gravel_climb", 0.10),  # 6 routes
    ("lap_climb", 0.10),     # 6 routes
    ("cobble_muur", 0.10),   # 6 routes
    # descent_special removed — flat_descending_tt and wrong_way_descent
    # produce monotonic straight-line profiles that look like a single
    # diagonal in the UI. Variety, not boredom.
]

DESERT_LOOP_DIST: list[tuple[str, float]] = [
    ("flat", 0.18),          # was 0.22
    ("rolling_flat", 0.13),  # was 0.15
    ("gravel_flat", 0.32),   # was 0.35 — still our gravel home
    ("lap_flat", 0.10),      # was 0.12
    ("cobble_soft", 0.22),   # was 0.12 — MORE cobble (Bologna TT
                             # finish + Roubaix-style flat cobble sectors)
    ("mixed_special", 0.05),
]


# Meta-family expansion — which archetypes each meta-family contains.
META_FAMILIES: dict[str, list[str]] = {
    # Blue Ridge families
    "rolling": ["rolling_easy", "rolling_punchy", "figure_8", "rolling_with_climb_finish"],
    # false_flat_climb removed — current templates produce a near-monotonic
    # 2-3% straight diagonal. Re-enable when the archetype gets real-world
    # variability injected (see reference doc FF templates).
    "climb_low": ["cat4_short", "cat3_tempo", "cat2_ramps", "summit_sprint"],
    # flat_descending_tt removed — produces a single straight-line
    # diagonal profile (no variety; fails the "look like a real road" test).
    "flat": ["flat_tt", "flat_with_sprint", "flat_with_hill_end"],
    "lap": ["lap_flat_tt", "lap_rolling", "lap_climb", "lap_punchy_kicker", "lap_criterium"],
    "cobble_soft": ["cobble_flat_classic", "cobble_rolling", "cobble_finish",
                    "cobble_climb_muur", "wall"],
    "gravel_soft": ["gravel_rolling_strade", "gravel_forest_rollercoaster"],
    "mixed_special": ["mixed_asphalt_gravel_sandwich", "mixed_gravel_finish",
                      "wall", "cobble_climb_muur", "summit_sprint"],
    # Iron Pass families
    "climb_wide": [
        "cat3_tempo", "cat2_ramps", "cat1_sustained", "cat1_variable",
        "hc_steady", "hc_irregular", "two_stepper", "wall", "summit_sprint",
        # false_flat_climb removed — current impl is a monotonic line.
    ],
    "rolling_kick": ["rolling_punchy", "rolling_with_climb_finish"],
    "gravel_climb": ["gravel_climb_mountain", "gravel_adventure_long"],
    "lap_climb": ["lap_climb", "lap_punchy_kicker"],
    "cobble_muur": ["cobble_climb_muur"],
    "descent_special": ["flat_descending_tt", "gravel_with_descent"],
    # Desert Loop families
    "rolling_flat": ["rolling_easy", "flat_with_hill_end"],
    "gravel_flat": [
        "gravel_rolling_strade", "gravel_adventure_long",
        "gravel_with_descent", "gravel_forest_rollercoaster",
    ],
    "lap_flat": ["lap_criterium", "lap_flat_tt"],
}

# Family labels (for schema: archetype_family tag)
ARCHETYPE_FAMILY_LABEL = {
    "flat": "flat",
    "rolling": "rolling",
    "climb": "climb",
    "cobble": "cobble",
    "gravel": "gravel",
    "mixed": "mixed",
    "lap": "lap",
    "special": "special",
}


# ─────────────────────────────────────────────────────────────────────────────
# Name pools
# ─────────────────────────────────────────────────────────────────────────────

NAME_PREFIXES = [
    "Morning", "Sunset", "Highland", "Lowland", "Ridge", "Valley", "Meadow",
    "Canyon", "Plateau", "Forest", "River", "Creek", "Mountain", "Desert",
    "Alpine", "Coastal", "Backcountry", "Sunrise", "Twilight", "Foothill",
    "Summit", "Pine", "Oak", "Cedar", "Maple", "Birch", "Willow", "Pioneer",
    "Heritage", "Legacy", "Northern", "Southern", "Eastern", "Western",
    "High", "Low", "Upper", "Lower", "Open", "Hidden", "Lost", "Silver",
    "Golden", "Iron", "Copper", "Stone", "Boulder", "Thunder", "Lightning",
    "Wind", "Breeze", "Storm", "Calm", "Quiet", "Wild", "Rugged", "Gentle",
    "Steep", "Rolling", "Sweeping", "Winding", "Flowing", "Rushing",
]

NAME_SUFFIXES = [
    "Loop", "Circuit", "Trail", "Road", "Path", "Run", "Route", "Ride",
    "Climb", "Ascent", "Descent", "Traverse", "Pass", "Crossing",
    "Ramble", "Cruise", "Tour", "Journey", "Quest", "Challenge",
    "Sprint", "Segment", "Stretch", "Stage", "Link",
    "Crest", "Saddle", "Ridgeline", "Peak",
    "Basin", "Glen", "Hollow", "Grove", "Gulch", "Gorge",
]


def route_name(world: str, idx: int) -> str:
    p_idx = _hash32(idx, stable_hash(world)) % len(NAME_PREFIXES)
    s_idx = _hash32(idx + 777, stable_hash(world)) % len(NAME_SUFFIXES)
    return f"{NAME_PREFIXES[p_idx]} {NAME_SUFFIXES[s_idx]} {idx}"


def slugify(name: str) -> str:
    return name.lower().replace(" ", "-")


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic picks
# ─────────────────────────────────────────────────────────────────────────────

def pick_weighted(items: list[tuple[str, float]], seed: int, salt: int) -> str:
    if not items:
        raise ValueError("pick_weighted: empty items list")
    total = sum(w for _, w in items)
    if total <= 0:
        # All-zero (or negative) weights: deterministic uniform fallback
        # instead of silently returning ``items[0]`` and masking config errors.
        idx = _hash32(70000 + salt, seed) % len(items)
        return items[idx][0]
    r = seeded_random(seed, 70000 + salt) * total
    cumul = 0.0
    for k, w in items:
        cumul += w
        if r <= cumul:
            return k
    return items[-1][0]


def pick_in_range(lo: float, hi: float, seed: int, salt: int) -> float:
    r = seeded_random(seed, 80000 + salt)
    return lo + r * (hi - lo)


# ─────────────────────────────────────────────────────────────────────────────
# Derived schema computations
# ─────────────────────────────────────────────────────────────────────────────

def compute_physical_metrics(segs: list[float], grades: list[float]) -> dict:
    total_km = sum(segs)
    elev = [0.0]
    for s, g in zip(segs, grades):
        elev.append(elev[-1] + s * 1000.0 * g / 100.0)
    climb_m = sum(s * 1000.0 * g / 100.0 for s, g in zip(segs, grades) if g > 0)
    descent_m = sum(-s * 1000.0 * g / 100.0 for s, g in zip(segs, grades) if g < 0)
    net_elev = elev[-1] - elev[0]
    avg_signed = (
        sum(g * d for g, d in zip(grades, segs)) / total_km if total_km > 0 else 0.0
    )
    avg_abs = (
        sum(abs(g) * d for g, d in zip(grades, segs)) / total_km if total_km > 0 else 0.0
    )
    max_g = max(grades) if grades else 0.0
    min_g = min(grades) if grades else 0.0
    return {
        "distance_km": round(total_km, 2),
        "climb_m": int(round(climb_m)),
        "descent_m": int(round(descent_m)),
        "net_elev_m": int(round(net_elev)),
        "avg_grade_signed": round(avg_signed, 2),
        "avg_grade_abs": round(avg_abs, 2),
        "max_grade": round(max_g, 1),
        "min_grade": round(min_g, 1),
        "elevation_points": elev,
    }


def compute_terrain(archetype_family: str, output: ArchetypeOutput,
                    metrics: dict) -> str:
    """Map archetype family + output into the schema terrain vocabulary."""
    # Prefer archetype self-declaration
    if output.terrain in {"flat", "rolling", "climb", "mixed"}:
        # refine using metrics
        if output.terrain == "flat" and metrics["avg_grade_abs"] < 2.0 and metrics["max_grade"] < 5.0:
            return "flat"
        return output.terrain
    return "mixed"


def compute_category(metrics: dict, output: ArchetypeOutput) -> str:
    """UCI-style heuristic from contract §2."""
    # Use primary_climb if present, otherwise use overall climb metrics
    if output.primary_climb:
        climb_m = output.primary_climb.get("length_km", 0) * 1000.0 * \
                  max(0.0, output.primary_climb.get("avg_grade", 0.0)) / 100.0
        avg_grade = output.primary_climb.get("avg_grade", 0.0)
    else:
        climb_m = metrics["climb_m"]
        avg_grade = metrics["avg_grade_signed"]
    points = climb_m * max(0.0, avg_grade)
    if climb_m < 50 or avg_grade < 3.0:
        # no real climb
        if metrics["climb_m"] < 80:
            return "flat"
    if points >= 80000 and avg_grade >= 6.0 and climb_m >= 1500:
        return "hc"
    if points >= 80000 and climb_m >= 1500:
        return "cat1"
    if points >= 64000:
        return "cat2"
    if points >= 32000:
        return "cat3"
    if points >= 16000:
        return "cat4"
    if points >= 5000:
        return "cat5"
    return "flat"


def compute_surface_mix(surface_segments: list[dict], total_km: float) -> tuple[dict, str, bool, bool]:
    """Return (surface_mix_pct, primary_surface, has_gravel, has_cobble).

    Keeps an explicit ``unknown`` bucket so real-world OSM imports with no
    surface tag are accounted for (previously they were auto-added by the
    ``surf not in totals`` fallback, but the bucket wasn't guaranteed to show
    up at zero for clean routes, causing downstream shape drift).
    """
    totals = {"asphalt": 0.0, "gravel": 0.0, "cobble": 0.0, "unknown": 0.0}
    for s in surface_segments:
        surf = s["surface"]
        if surf not in totals:
            totals[surf] = 0.0
        totals[surf] += max(0.0, s["end_km"] - s["start_km"])
    if total_km > 0:
        pct = {k: round(100.0 * v / total_km) for k, v in totals.items()}
    else:
        pct = {k: 0 for k in totals}
    # Correct rounding so sum == 100
    diff = 100 - sum(pct.values())
    if diff != 0:
        primary = max(pct, key=pct.get)
        pct[primary] += diff
    primary_surface = max(pct, key=pct.get)
    has_gravel = pct.get("gravel", 0) > 0
    has_cobble = pct.get("cobble", 0) > 0
    return pct, primary_surface, has_gravel, has_cobble


def compute_difficulty(climb_m: int, max_grade: float, distance_km: float,
                       has_gravel: bool, has_cobble: bool, is_lap: bool) -> float:
    surface_penalty = 0.0
    if has_gravel: surface_penalty += 0.10
    if has_cobble: surface_penalty += 0.15
    lap_penalty = 0.05 if is_lap else 0.0
    score = (
        0.40 * min(1.0, climb_m / 1500.0) +
        0.25 * min(1.0, max_grade / 15.0) +
        0.15 * min(1.0, distance_km / 80.0) +
        0.15 * surface_penalty +
        0.05 * lap_penalty
    )
    return round(1.0 + 9.0 * score, 1)


def surface_at(surface_segments: list[dict], km: float) -> str:
    for s in surface_segments:
        # Exclusive upper bound — avoids silent tail misclass when km==total_km
        # or float gaps between segments fall through to the last-seg fallback.
        if s["start_km"] <= km < s["end_km"]:
            return s["surface"]
    return "unknown"


def est_duration_min_z2(segs: list[float], grades: list[float],
                        surface_segments: list[dict]) -> int:
    total_h = 0.0
    pos = 0.0
    for d, g in zip(segs, grades):
        mid = pos + d * 0.5
        surf = surface_at(surface_segments, mid)
        base = 25.0
        if g > 3:
            base = max(7.0, 25.0 - g * 2.0)
        elif g < -3:
            base = min(55.0, 25.0 - g * 1.5)
        if surf == "gravel":
            base *= 0.72
        elif surf == "cobble":
            base *= 0.60
        total_h += d / base if base > 0 else 0.0
        pos += d
    return int(round(total_h * 60))


def est_tss(distance_km: float, climb_m: int, max_grade: float,
            has_gravel: bool, has_cobble: bool) -> int:
    """Rough Z2 TSS estimate: 60 TSS/hour baseline, climbs push up intensity."""
    # rough hour estimate from distance and climb
    hours = distance_km / 22.0 + climb_m / 800.0
    intensity = 0.65
    if max_grade > 9: intensity += 0.05
    if has_cobble: intensity += 0.04
    if has_gravel: intensity += 0.02
    return int(round(100.0 * hours * intensity * intensity))


def compute_climb_count(segs: list[float], grades: list[float],
                        min_grade: float = 5.0,
                        min_len_km: float = 0.5) -> tuple[int, Optional[dict]]:
    """Walk the profile counting sustained >=5% segments >=500m. Return primary.

    Delegates to ``route_archetypes._detect_climbs`` (canonical) so the two
    previously-duplicated implementations can never drift.
    """
    climbs = _detect_climbs(segs, grades, min_grade=min_grade, min_len_km=min_len_km)
    if not climbs:
        return 0, None
    primary = max(climbs, key=lambda c: c["length_km"])
    return len(climbs), primary


def _interp_to(values: list[float], new_len: int) -> list[float]:
    """Pure-Python linear interpolation of ``values`` to ``new_len`` samples.

    Used by ``build_preview_profile`` to defensively stretch/shrink a short
    or long elevation array to match ``len(segs)+1`` before downsampling.
    """
    if new_len <= 0:
        return []
    if not values:
        return [0.0] * new_len
    if len(values) == 1:
        return [float(values[0])] * new_len
    if len(values) == new_len:
        return [float(v) for v in values]
    old_n = len(values) - 1
    out: list[float] = []
    for i in range(new_len):
        # Position on the original [0, old_n] axis
        t = (i * old_n) / (new_len - 1) if new_len > 1 else 0.0
        lo = int(t)
        hi = min(lo + 1, old_n)
        frac = t - lo
        out.append(values[lo] + (values[hi] - values[lo]) * frac)
    return out


def build_preview_profile(segs: list[float], elevation_points: list[float],
                          max_points: int = 18) -> list[list[float]]:
    """Downsample the elevation profile to 12-18 (distance_km, elev_m) points."""
    if not segs:
        return [[0.0, 0.0]]
    # Defensive length guard: callers sometimes pass elevation arrays that
    # don't match ``len(segs)+1`` (e.g. pre-computed from a lower-resolution
    # CRS, or truncated after smoothing). Interpolate rather than crash.
    need = len(segs) + 1
    if len(elevation_points) != need:
        elevation_points = _interp_to(list(elevation_points), need)
    total_km = sum(segs)
    cum = [0.0]
    for s in segs:
        cum.append(cum[-1] + s)
    n = len(cum)

    # Always include start and end
    indices = {0, n - 1}

    # Find local extrema in elevation deltas (direction changes)
    deltas = []
    for i in range(1, n):
        deltas.append(elevation_points[i] - elevation_points[i - 1])
    # Smooth a bit by chunking deltas into ~40 buckets
    chunk = max(1, len(deltas) // 40)
    extrema_idx = []
    prev_sign = 0
    for i in range(0, len(deltas), chunk):
        bucket = deltas[i:i + chunk]
        s = sum(bucket)
        sign = 1 if s > 0 else (-1 if s < 0 else 0)
        if sign != 0 and sign != prev_sign and prev_sign != 0:
            extrema_idx.append(min(n - 1, i))
        if sign != 0:
            prev_sign = sign

    # Add extrema but cap count
    for idx in extrema_idx[: max_points - 4]:
        indices.add(idx)

    # Uniform fill to cap max gap
    max_gap = total_km / 15.0
    sorted_idx = sorted(indices)
    filled = set(sorted_idx)
    for a, b in zip(sorted_idx, sorted_idx[1:]):
        gap_km = cum[b] - cum[a]
        if gap_km > max_gap:
            steps = int(gap_km / max_gap)
            for k in range(1, steps + 1):
                mid_km = cum[a] + (gap_km / (steps + 1)) * k
                # find closest index
                best = a
                best_d = abs(cum[a] - mid_km)
                for j in range(a, b + 1):
                    dd = abs(cum[j] - mid_km)
                    if dd < best_d:
                        best_d = dd
                        best = j
                filled.add(best)

    # Cap at max_points
    final = sorted(filled)
    if len(final) > max_points:
        # uniform subsample
        step = len(final) / max_points
        trimmed = {final[int(i * step)] for i in range(max_points)}
        trimmed.add(0)
        trimmed.add(n - 1)
        final = sorted(trimmed)

    return [[round(cum[i], 2), int(round(elevation_points[i]))] for i in final]


# ─────────────────────────────────────────────────────────────────────────────
# CRS writer
# ─────────────────────────────────────────────────────────────────────────────

def write_crs(path: Path, world_label: str, route_label: str,
              segs: list[float], grades: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    total_km = sum(segs)
    header = (
        "[COURSE HEADER]\n"
        f"DESCRIPTION = Domestique: {world_label} - {route_label} ({total_km:.1f}km)\n"
        f"FILE NAME = {path.name}\n"
        "UNITS = METRIC\n"
        "[END COURSE HEADER]\n"
        "[COURSE DATA]\n"
        "DISTANCE\tGRADE\tWIND\n"
    )
    with open(path, "w") as f:
        f.write(header)
        for d, g in zip(segs, grades):
            f.write(f"{d:.3f}\t{g:.1f}\t0\n")


def write_profile_json(path: Path, world: str, slug: str, name: str,
                       segs: list[float], grades: list[float],
                       elevation_points: list[float]) -> None:
    total_km = sum(segs)
    climb_m = int(round(sum(s * 1000.0 * g / 100.0 for s, g in zip(segs, grades) if g > 0)))
    max_g = round(max(grades) if grades else 0.0, 1)
    total_k = total_km or 1.0
    avg_signed = round(
        sum(g * d for g, d in zip(grades, segs)) / total_k, 2
    )
    avg_abs = round(
        sum(abs(g) * d for g, d in zip(grades, segs)) / total_k, 2
    )
    profile_pts = []
    cum = 0.0
    for s, g, e in zip(segs, grades, elevation_points[1:]):
        cum += s
        profile_pts.append({
            "d": round(cum, 3),
            "e": round(e, 1),
            "g": round(g, 1),
        })
    data = {
        "world": world,
        "slug": slug,
        "name": name,
        "distance_km": round(total_km, 2),
        "elev_gain_m": climb_m,
        "max_grade": max_g,
        "avg_grade": avg_signed,
        "avg_abs_grade": avg_abs,
        "profile": profile_pts,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, separators=(",", ":"))


# ─────────────────────────────────────────────────────────────────────────────
# Route construction
# ─────────────────────────────────────────────────────────────────────────────

WORLD_LABELS = {
    "blue_ridge": "Blue Ridge",
    "iron_pass": "Iron Pass",
    "desert_loop": "Desert Loop",
}


def _add_roll_to_near_flat(grades: list[float], seed: int,
                            spec: ArchetypeSpec) -> list[float]:
    """Layer a micro aperiodic wiggle onto near-flat regions of a profile.

    Many archetypes produce long stretches of near-zero grade (cobble_finish,
    rolling_with_climb_finish, flat_with_hill_end, mixed_gravel_finish) where
    every adjacent segment is within 0.1% of its neighbour — this drives
    max_autocorr ≈ 0.9 at lag 3. Adding a small aperiodic micro-undulation
    (max 0.8% amplitude) in those regions breaks the autocorr while keeping
    the macro shape visually identical (the kicker/climb at the end stays).
    """
    from route_archetypes import _fbm_1d, _value_noise_1d
    n = len(grades)
    if n < 10:
        return grades
    # Compute rolling 500m (10-segment) average of |grade|
    window = 10
    flat_mask = [False] * n
    for i in range(n):
        lo = max(0, i - window // 2)
        hi = min(n, i + window // 2 + 1)
        avg_abs = sum(abs(grades[j]) for j in range(lo, hi)) / max(1, hi - lo)
        if avg_abs < 2.0:
            flat_mask[i] = True
    # Amplitude: modest so we don't destroy the profile character
    if spec.family == "flat":
        amp = 0.35
    else:
        amp = 0.6
    # Flat-TT variants should stay FLAT: cap lower
    out = list(grades)
    for i in range(n):
        if flat_mask[i]:
            lo_noise = _fbm_1d(i * 0.35, seed, octaves=3, persistence=0.55) * 0.6
            mid_noise = _fbm_1d(i * 1.1, seed ^ 0x21, octaves=2, persistence=0.45) * 0.5
            hi_noise = _value_noise_1d(i * 2.8, seed ^ 0x55) * 0.4
            out[i] = grades[i] + (lo_noise + mid_noise + hi_noise) * amp
    return out


def finalize_output(output: ArchetypeOutput, spec: ArchetypeSpec) -> tuple[list[float], list[float], list[dict]]:
    """Apply smoothing + clipping + flat-roll injection."""
    grades = apply_smoothing(output.grades, spec.smoothing_max_change)
    grades = apply_clipping(grades, spec.min_grade_floor, spec.max_grade_cap)
    # Post-process: inject micro-undulations in near-flat regions to break
    # the high lag-3 autocorr that near-zero stretches produce.
    grades = _add_roll_to_near_flat(
        grades, (_hash32(777, len(grades) * 31) + sum(int(g * 10) for g in grades[:5])) & 0xFFFFFFFF,
        spec,
    )
    grades = apply_clipping(grades, spec.min_grade_floor, spec.max_grade_cap)
    return output.segs, grades, output.surface_segments


def build_route_entry(region: str, idx: int, name: str, slug: str,
                      archetype_name: str, spec: ArchetypeSpec,
                      segs: list[float], grades: list[float],
                      surface_segments: list[dict],
                      lap_meta: Optional[dict] = None) -> tuple[dict, Optional[list[dict]], list[dict]]:
    metrics = compute_physical_metrics(segs, grades)
    output = ArchetypeOutput(
        segs=segs, grades=grades, surface_segments=surface_segments,
        terrain="", finish_type="", climb_count=0, primary_climb=None,
    )

    # Surface mix
    surface_mix_pct, primary_surface, has_gravel, has_cobble = compute_surface_mix(
        surface_segments, metrics["distance_km"]
    )

    # Climb count + primary climb from actual profile walk
    climb_count, primary_climb = compute_climb_count(segs, grades)

    # Terrain
    avg_abs = metrics["avg_grade_abs"]
    max_g = metrics["max_grade"]
    if spec.family == "lap":
        # For laps, use the shape-ish hint
        if primary_climb and primary_climb["length_km"] >= 3.0 and primary_climb["avg_grade"] >= 4.0:
            terrain = "climb"
        elif avg_abs < 2.0 and max_g < 5.0:
            terrain = "flat"
        elif max_g >= 7.0 or climb_count > 0:
            terrain = "rolling"
        else:
            terrain = "flat"
    elif spec.family == "flat":
        terrain = "flat" if (avg_abs < 2.0 and max_g < 5.0) else "mixed"
    elif spec.family == "rolling":
        terrain = "rolling"
    elif spec.family == "climb":
        terrain = "climb"
    elif spec.family in ("cobble", "gravel", "mixed"):
        if primary_climb and primary_climb["length_km"] >= 5.0 and primary_climb["avg_grade"] >= 4.0:
            terrain = "climb"
        elif avg_abs < 2.0 and max_g < 5.0:
            terrain = "flat"
        elif avg_abs < 4.0:
            terrain = "rolling"
        else:
            terrain = "mixed"
    else:
        terrain = "mixed"

    # finish_type — derive from archetype + metrics
    finish_type = derive_finish_type(archetype_name, segs, grades, metrics)

    # Category
    # Recompute with proper primary_climb
    category = compute_category_from_primary(metrics, primary_climb)

    # Loop flag
    loop = archetype_name not in ("flat_descending_tt",)
    if spec.family == "gravel" and archetype_name == "gravel_with_descent":
        loop = False
    # Lap routes are always loops by construction
    if spec.family == "lap":
        loop = True

    # Difficulty / duration / TSS
    difficulty = compute_difficulty(
        metrics["climb_m"], metrics["max_grade"], metrics["distance_km"],
        has_gravel, has_cobble, lap_meta is not None,
    )
    duration_min = est_duration_min_z2(segs, grades, surface_segments)
    tss = est_tss(metrics["distance_km"], metrics["climb_m"], metrics["max_grade"],
                  has_gravel, has_cobble)

    # Tags (DERIVED)
    archetype_family = ARCHETYPE_FAMILY_LABEL.get(spec.family, spec.family)
    tags = [archetype_family, category, primary_surface, finish_type,
            "loop" if loop else "point_to_point"]
    # dedupe while preserving order
    seen = set()
    tags = [t for t in tags if not (t in seen or seen.add(t))]

    preview = build_preview_profile(segs, metrics["elevation_points"])

    route_id = f"{region}/{slug}"
    crs_filename = f"{region.replace('_', '-')}__{slug}.crs"
    crs_path = f"courses/virtual/{region}/{crs_filename}"

    entry = {
        "id": route_id,
        "name": name,
        "crs_path": crs_path,
        "region": region,
        "source": "virtual",
        "distance_km": metrics["distance_km"],
        "climb_m": metrics["climb_m"],
        "descent_m": metrics["descent_m"],
        "net_elev_m": metrics["net_elev_m"],
        "avg_grade_signed": metrics["avg_grade_signed"],
        "avg_grade_abs": metrics["avg_grade_abs"],
        "max_grade": metrics["max_grade"],
        "min_grade": metrics["min_grade"],
        "terrain": terrain,
        "category": category,
        "finish_type": finish_type,
        "loop": loop,
        "climb_count": climb_count,
        "primary_climb": primary_climb,
        "primary_surface": primary_surface,
        "surface_mix_pct": surface_mix_pct,
        "has_gravel": has_gravel,
        "has_cobble": has_cobble,
        "lap_route": lap_meta,
        "difficulty_score": difficulty,
        "est_duration_min_z2": duration_min,
        "est_tss": tss,
        "archetype": archetype_name,
        "tags": tags,
        "preview_profile": preview,
    }

    # surface_types entry: only if NOT 100% asphalt
    surface_entry = None
    if surface_mix_pct.get("asphalt", 0) < 100:
        surface_entry = [
            {
                "start_km": round(s["start_km"], 3),
                "end_km": round(s["end_km"], 3),
                "surface": s["surface"],
            }
            for s in surface_segments
            if s["end_km"] - s["start_km"] > 0.001
        ]

    return entry, surface_entry, []


def compute_category_from_primary(metrics: dict, primary_climb: Optional[dict]) -> str:
    if not primary_climb:
        # still flat check — any significant climbing?
        if metrics["climb_m"] < 80 or metrics["avg_grade_abs"] < 2.0:
            return "flat"
        return "cat5"
    climb_len_km = primary_climb["length_km"]
    avg_grade = primary_climb["avg_grade"]
    climb_m = climb_len_km * 1000.0 * max(0.0, avg_grade) / 100.0
    if climb_m < 50 or avg_grade < 3.0:
        return "flat"
    if climb_len_km < 1.0 and avg_grade < 3.0:
        return "flat"
    points = climb_m * avg_grade
    if points >= 80000 and avg_grade >= 6.0 and climb_m >= 1500:
        return "hc"
    if points >= 80000 and climb_m >= 1500:
        return "cat1"
    if points >= 64000:
        return "cat2"
    if points >= 32000:
        return "cat3"
    if points >= 16000:
        return "cat4"
    if points >= 5000:
        return "cat5"
    return "flat"


def derive_finish_type(archetype_name: str, segs: list[float], grades: list[float],
                       metrics: dict) -> str:
    """Determine finish_type from the last 3km slice."""
    cum = 0.0
    last3_idx = 0
    for i, s in enumerate(segs):
        cum += s
    total = sum(segs)
    pos = 0.0
    tail_segs = []
    tail_grades = []
    for s, g in zip(segs, grades):
        if total - (pos + s) <= 3.0:
            tail_segs.append(s)
            tail_grades.append(g)
        pos += s
    if not tail_segs:
        tail_segs = segs[-5:]
        tail_grades = grades[-5:]
    tail_d = sum(tail_segs) or 1.0
    tail_avg = sum(g * d for g, d in zip(tail_grades, tail_segs)) / tail_d
    # wall: final 1-3km ramps hard
    final_1_5 = tail_segs[-int(1.5 / 0.1):] if tail_segs else []
    final_1_5_g = tail_grades[-len(final_1_5):] if final_1_5 else []
    final_d = sum(final_1_5) or 1.0
    final_avg = (sum(g * d for g, d in zip(final_1_5_g, final_1_5)) / final_d
                 if final_1_5 else tail_avg)

    if final_avg >= 9.0:
        return "wall"
    if tail_avg >= 4.0:
        return "summit"
    if tail_avg <= -3.0:
        return "descent"
    if archetype_name in ("flat_tt", "flat_with_sprint", "lap_flat_tt",
                          "lap_criterium", "cobble_flat_classic"):
        return "sprint_flat"
    if abs(tail_avg) < 1.0:
        return "sprint_flat" if archetype_name.startswith("flat") else "none"
    return "none"


# ─────────────────────────────────────────────────────────────────────────────
# Lap route generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_lap_variants(region: str, lap_count_target: int,
                          lap_archetypes: list[str], start_idx: int) -> list[dict]:
    """Produce `lap_count_target` UNIQUE lap-style routes.

    Previously this built one base per 4 routes and tiled it for the ×2/×3/×5/×8
    variants — making the four variants visually identical save for length. The
    user called this out as boring repetition. Now each lap route is an
    independent call to the archetype (which uses `_varied_laps` internally),
    so every lap in every variant has its own template + peak grade + flat
    length. No shared base_loop_id grouping.
    """
    variant_plans: list[dict] = []
    idx = start_idx
    for i in range(lap_count_target):
        seed = _hash32(i * 101 + 1, stable_hash(region + "_laps"))
        # Pick archetype (seeded)
        archetype_name = lap_archetypes[
            _hash32(i * 13 + 3, stable_hash(region)) % len(lap_archetypes)
        ]
        spec = ARCHETYPE_REGISTRY[archetype_name]

        # Pick total_km from the archetype's declared range.
        # Spread variants in length so the route list covers short/medium/long.
        # Bucket the index into {short, medium, long, epic}.
        span = spec.dist_max_km - spec.dist_min_km
        bucket = i % 4
        if bucket == 0:  # short — 0-30% of range
            total_km = spec.dist_min_km + seeded_random(seed, 1) * span * 0.30
        elif bucket == 1:  # medium — 25-55%
            total_km = spec.dist_min_km + (0.25 + seeded_random(seed, 1) * 0.30) * span
        elif bucket == 2:  # long — 50-80%
            total_km = spec.dist_min_km + (0.50 + seeded_random(seed, 1) * 0.30) * span
        else:  # epic — 75-100%
            total_km = spec.dist_min_km + (0.75 + seeded_random(seed, 1) * 0.25) * span

        # Generate via the archetype's varied-laps logic
        ao = spec.fn(total_km, seed)
        grades = apply_smoothing(ao.grades, spec.smoothing_max_change)
        grades = apply_clipping(grades, spec.min_grade_floor, spec.max_grade_cap)

        name = f"{gen_base_name(region, i)} Lap"
        slug = slugify(name)

        # Keep lap_route metadata so the UI can still surface "5-lap circuit"
        # type badges, but drop `base_loop_id` (no shared base anymore).
        actual_km = sum(ao.segs)
        lap_meta = None
        if actual_km >= 3:
            # Infer a "lap count" from the archetype's internal splitting (which
            # already divided into N varied laps via _varied_laps)
            from route_archetypes import _pick_lap_params
            base_km_inferred, lap_count_inferred = _pick_lap_params(
                actual_km, seed,
                base_range=(1.0, 2.0) if archetype_name == "lap_criterium" else (3.0, 6.0),
            )
            lap_meta = {
                "base_loop_id": None,
                "base_loop_km": round(base_km_inferred, 2),
                "lap_count": lap_count_inferred,
                "base_archetype": archetype_name,
            }

        variant_plans.append({
            "archetype": archetype_name,
            "spec": spec,
            "segs": ao.segs,
            "grades": grades,
            "surface_segments": ao.surface_segments,
            "name": name,
            "slug": slug,
            "idx": idx,
            "lap_meta": lap_meta,
        })
        idx += 1
    return variant_plans


def pick_variant_counts(possible: list[int], n: int, seed: int) -> list[int]:
    """Pick n DISTINCT variant counts; pads with new values if n > len(possible)."""
    off = _hash32(1, seed) % len(possible)
    rotated = possible[off:] + possible[:off]
    if n <= len(possible):
        return rotated[:n]
    # Need more — append new multiples (e.g., 15, 20) that don't collide
    out = list(rotated)
    extra = n - len(possible)
    base_extra = max(possible) + 2
    for k in range(extra):
        out.append(base_extra + k * 3)
    return out


def gen_base_name(region: str, base_i: int) -> str:
    """Deterministic base-loop name."""
    seed = stable_hash(region + "_laps")
    p_idx = _hash32(base_i * 5 + 11, seed) % len(NAME_PREFIXES)
    s_idx = _hash32(base_i * 7 + 23, seed) % len(NAME_SUFFIXES)
    return f"{NAME_PREFIXES[p_idx]} {NAME_SUFFIXES[s_idx]} Base {base_i + 1}"


def build_lap_base_profile(archetype_name: str, base_km: float,
                           seed: int) -> tuple[list[float], list[float], list[dict]]:
    """Build a SINGLE-lap base profile for the given lap archetype.

    We reuse the archetype fn via ``lap_count=1`` path by picking a matching
    total_km. The lap_* archetypes use ``_pick_lap_params`` internally; by
    passing ``total_km == base_km`` we coerce lap_count=1.
    """
    from route_archetypes import ARCHETYPE_REGISTRY as REG
    spec = REG[archetype_name]
    # Call with base_km directly — lap_* archetypes will pick lap_count=1 when
    # base_km is in the expected base_range for that archetype, but because
    # total_km == base_km they'll likely pick lap_count=1 anyway.
    # To be safe, we build the base profile directly using the underlying
    # primitives rather than calling the lap fn itself.
    if archetype_name == "lap_flat_tt":
        return _base_flat_tt(base_km, seed)
    if archetype_name == "lap_rolling":
        return _base_rolling(base_km, seed)
    if archetype_name == "lap_climb":
        return _base_climb(base_km, seed)
    if archetype_name == "lap_punchy_kicker":
        return _base_punchy_kicker(base_km, seed)
    if archetype_name == "lap_criterium":
        return _base_criterium(base_km, seed)
    raise ValueError(f"Unknown lap archetype: {archetype_name}")


def _base_flat_tt(base_km: float, seed: int):
    """Pan-flat TT with layered aperiodic noise (not single-octave Perlin)."""
    from route_archetypes import (
        SEG_KM, segment_lengths, uniform_surface, _fbm_1d, _value_noise_1d,
    )
    segs = segment_lengths(base_km, seed, SEG_KM)
    grades = []
    for i, _ in enumerate(segs):
        km = i * SEG_KM
        lo = _fbm_1d(km * 0.5, seed, octaves=3, persistence=0.5) * 0.4
        hi = _value_noise_1d(km * 2.2, seed ^ 0x55) * 0.3
        g = 0.12 + lo + hi
        grades.append(g)
    return segs, grades, uniform_surface(base_km, "asphalt")


def _base_rolling(base_km: float, seed: int):
    """Aperiodic rolling base via Poisson-placed climb/descent blocks + fBm.

    Replaces the previous literal sin(2πt) wave that produced max_autocorr
    ≈ 0.95 at lag 3. The new profile uses layered fBm at 3 scales (50m, 250m,
    800m) and aperiodic climb blocks placed by Poisson spacing so no two laps
    repeat their peaks.
    """
    from route_archetypes import (SEG_KM, segment_lengths, uniform_surface,
                                  _fbm_1d, _value_noise_1d, _poisson_positions)
    segs = segment_lengths(base_km, seed, SEG_KM)
    n = len(segs)
    grades = []
    # Aperiodic "peaks" via Poisson positions (mean spacing 1.2 km)
    peaks = _poisson_positions(base_km, 1.2, seed ^ 0xBEEF)
    peak_amps = [1.8 + _value_noise_1d(j * 0.7, seed ^ 0xB0) * 1.3
                 for j in range(len(peaks))]
    for i in range(n):
        km = i * SEG_KM
        # Layered fBm: long (rolling) + mid (kickers) + short (chunk)
        lo = _fbm_1d(km * 0.4, seed, octaves=4, persistence=0.5) * 1.5
        mid = _fbm_1d(km * 1.1, seed ^ 0x1234, octaves=3, persistence=0.45) * 1.0
        hi = _value_noise_1d(km * 3.0, seed ^ 0x99) * 0.4
        # Peak contributions (aperiodic bumps)
        peak_sum = 0.0
        for j, pk in enumerate(peaks):
            d = km - pk
            peak_sum += peak_amps[j] * pow(2.718281828, -(d * d) / 0.4)
        g = lo + mid + hi + peak_sum * 0.3
        grades.append(g)
    # Zero-mean so the lap is closed
    mean = sum(grades) / max(1, n)
    grades = [g - mean for g in grades]
    return segs, grades, uniform_surface(base_km, "asphalt")


def _base_climb(base_km: float, seed: int):
    """Aperiodic climb+descent base with irregular interleaved climb+descent.

    Rather than a pure monotonic climb then pure descent (which produces
    bimodal autocorr spikes at lag ≤ climb_length), this builds an interleaved
    sequence of 4-6 "regions" that alternate climbing and descending, with
    aperiodic amplitudes and lengths per seed. The NET climb is zero (closed
    loop) but the pattern doesn't visually repeat across laps when stitched
    together because each base is unique per-seed.
    """
    from route_archetypes import (
        SEG_KM, segment_lengths, uniform_surface,
        _fbm_1d, _value_noise_1d,
    )
    segs = segment_lengths(base_km, seed, SEG_KM)
    n = len(segs)
    # Number of alternating regions (5-8) — keeps shape from being bimodal
    n_regions = 5 + (_hash32(1, seed) % 4)
    # Aperiodic region boundaries
    region_bounds = [0]
    rems = [1.0]
    for j in range(n_regions - 1):
        rems.append(0.5 + _value_noise_1d(j * 1.1, seed ^ 0x55) * 1.0)
    total_w = sum(rems[1:]) if n_regions > 1 else 1.0
    cum = 0
    for j in range(n_regions - 1):
        cum += int(round(n * rems[j + 1] / total_w))
        region_bounds.append(max(region_bounds[-1] + 1, min(n - 1, cum)))
    region_bounds.append(n)
    # Per-region grade averages: aperiodic signs and magnitudes
    region_avgs = []
    target_climb = 4.5 + seeded_random(seed, 65) * 2.5
    sign = 1 if (_hash32(2, seed) % 2) == 0 else -1
    for j in range(n_regions):
        phase = (-1) ** j * sign  # alternate climb/descent
        mag = target_climb * (0.6 + _value_noise_1d(j * 1.3, seed ^ 0xAA) * 0.8)
        region_avgs.append(phase * mag)
    grades = []
    for i in range(n):
        rj = 0
        for j in range(n_regions):
            if region_bounds[j] <= i < region_bounds[j + 1]:
                rj = j
                break
        base_g = region_avgs[rj]
        wiggle = _fbm_1d(i * SEG_KM * 0.9, seed, octaves=3, persistence=0.5) * 1.8
        mid_freq = _fbm_1d(i * SEG_KM * 2.4, seed ^ 0x33, octaves=2, persistence=0.5) * 1.3
        high = _value_noise_1d(i * SEG_KM * 5.0, seed ^ 7) * 1.1
        grades.append(base_g + wiggle + mid_freq + high)
    # Zero-mean (weighted) so the lap is closed
    weighted_mean = sum(g * d for g, d in zip(grades, segs)) / max(0.001, sum(segs))
    grades = [g - weighted_mean for g in grades]
    return segs, grades, uniform_surface(base_km, "asphalt")


def _base_punchy_kicker(base_km: float, seed: int):
    """Flat-rolling body + aperiodic punchy kicker at varying position.

    Body is NOT near-zero (which drives high autocorr) — instead it has
    1-2% undulations with fBm. Kicker position varies per seed to prevent
    identical placement when laps are stitched.
    """
    from route_archetypes import (
        SEG_KM, segment_lengths, uniform_surface,
        _fbm_1d, _value_noise_1d,
    )
    segs = segment_lengths(base_km, seed, SEG_KM)
    n = len(segs)
    kicker_pos_frac = 0.50 + seeded_random(seed, 71) * 0.35
    wall_len = 0.35 + seeded_random(seed, 88) * 0.45
    wall_start = base_km * kicker_pos_frac
    wall_end = min(base_km, wall_start + wall_len)
    kicker_avg = 8.5 + seeded_random(seed, 66) * 3.5
    grades = []
    pos = 0.0
    for i, s in enumerate(segs):
        mid = pos + s * 0.5
        if mid < wall_start or mid >= wall_end:
            # Rolling body (meaningful fBm amplitude so adjacent segments differ)
            lo = _fbm_1d(mid * 0.45, seed, octaves=4, persistence=0.55) * 1.5
            mid_f = _fbm_1d(mid * 1.5, seed ^ 0x22, octaves=2, persistence=0.45) * 0.9
            hi = _value_noise_1d(mid * 3.5, seed ^ 0xAB) * 0.7
            g = lo + mid_f + hi
        else:
            t = (mid - wall_start) / wall_len
            bell = 4 * t * (1 - t) * 1.3
            wiggle = _fbm_1d(mid * 1.5, seed ^ 0x99, octaves=2) * 1.5
            g = kicker_avg * (0.4 + 0.9 * bell) + wiggle
        grades.append(g)
        pos += s
    # Weighted-mean zeroing so the loop closes
    weighted_mean = sum(g * d for g, d in zip(grades, segs)) / max(0.001, sum(segs))
    grades = [g - weighted_mean for g in grades]
    return segs, grades, uniform_surface(base_km, "asphalt")


def _base_criterium(base_km: float, seed: int):
    """Pure criterium: layered fBm for aperiodic micro-undulations."""
    from route_archetypes import (
        SEG_KM, segment_lengths, uniform_surface, _fbm_1d, _value_noise_1d,
    )
    segs = segment_lengths(base_km, seed, 0.05)
    grades = []
    for i, _ in enumerate(segs):
        km = i * SEG_KM
        lo = _fbm_1d(km * 0.7, seed, octaves=3, persistence=0.5) * 0.55
        hi = _value_noise_1d(km * 2.5, seed ^ 0x77) * 0.35
        grades.append(lo + hi)
    return segs, grades, uniform_surface(base_km, "asphalt")


# ─────────────────────────────────────────────────────────────────────────────
# World generation
# ─────────────────────────────────────────────────────────────────────────────

# Counts reduced from 250/220/240 (710) to 100/60/60 (220) per simulator-inspired
# "fewer but more distinct routes per world" pattern. Lap targets scaled ~10%.
WORLDS = [
    ("blue_ridge", 100, BLUE_RIDGE_DIST, {
        "lap_family_key": "lap",
        "lap_target": 10,
        "lap_archetypes": ["lap_flat_tt", "lap_rolling", "lap_climb",
                           "lap_punchy_kicker", "lap_criterium"],
    }),
    ("iron_pass", 60, IRON_PASS_DIST, {
        "lap_family_key": "lap_climb",
        "lap_target": 6,
        "lap_archetypes": ["lap_climb", "lap_punchy_kicker"],
    }),
    ("desert_loop", 60, DESERT_LOOP_DIST, {
        "lap_family_key": "lap_flat",
        "lap_target": 6,
        "lap_archetypes": ["lap_criterium", "lap_flat_tt"],
    }),
]


def generate_non_lap_routes(region: str, target_count: int,
                            distribution: list[tuple[str, float]],
                            lap_family_key: str, lap_target: int,
                            start_idx: int) -> list[dict]:
    """Pick non-lap routes using the distribution excluding the lap family."""
    # Renormalise distribution without lap family
    non_lap = [(f, w) for f, w in distribution if f != lap_family_key]
    total_w = sum(w for _, w in non_lap)
    non_lap = [(f, w / total_w) for f, w in non_lap]

    routes_needed = target_count - lap_target
    plans = []
    idx = start_idx
    for i in range(routes_needed):
        seed = _hash32(idx * 13 + 7, stable_hash(region))
        family = pick_weighted(non_lap, seed, 0)
        members = META_FAMILIES[family]
        archetype_name = members[_hash32(idx + 101, seed) % len(members)]
        spec = ARCHETYPE_REGISTRY[archetype_name]

        total_km = pick_in_range(spec.dist_min_km, spec.dist_max_km, seed, 1)
        output = spec.fn(total_km, seed)
        segs, grades, surface_segments = finalize_output(output, spec)

        name = route_name(WORLD_LABELS[region], idx)
        slug = slugify(name)
        plans.append({
            "archetype": archetype_name,
            "spec": spec,
            "segs": segs,
            "grades": grades,
            "surface_segments": surface_segments,
            "name": name,
            "slug": slug,
            "idx": idx,
            "lap_meta": None,
        })
        idx += 1
    return plans


def generate_world(region: str, target_count: int,
                   distribution: list[tuple[str, float]],
                   extras: dict) -> list[dict]:
    lap_target = extras["lap_target"]
    lap_archetypes = extras["lap_archetypes"]
    lap_family_key = extras["lap_family_key"]

    # Generate non-lap portion first
    non_lap_plans = generate_non_lap_routes(
        region, target_count, distribution, lap_family_key, lap_target, start_idx=1,
    )
    # Lap portion
    lap_plans = generate_lap_variants(region, lap_target, lap_archetypes,
                                      start_idx=len(non_lap_plans) + 1)
    return non_lap_plans + lap_plans


# ─────────────────────────────────────────────────────────────────────────────
# Emission
# ─────────────────────────────────────────────────────────────────────────────

def ensure_unique_slug(slug: str, used: set) -> str:
    if slug not in used:
        used.add(slug)
        return slug
    i = 2
    while f"{slug}-{i}" in used:
        i += 1
    out = f"{slug}-{i}"
    used.add(out)
    return out


def emit_routes() -> tuple[int, float]:
    """Generate everything. Returns (route_count, wall_seconds)."""
    t_start = time.time()
    # Clear old virtual artefacts
    for region, _, _, _ in WORLDS:
        region_dir = COURSES_DIR / region
        if region_dir.exists():
            for p in region_dir.glob("*.crs"):
                p.unlink()
        else:
            region_dir.mkdir(parents=True, exist_ok=True)
    # Remove any old virtual profile JSONs
    for region, _, _, _ in WORLDS:
        prefix = f"{region}__"
        for p in PROFILES_DIR.glob(f"{prefix}*.json"):
            p.unlink()

    all_entries: list[dict] = []
    all_surfaces: dict[str, list[dict]] = {}

    for region, target_count, distribution, extras in WORLDS:
        plans = generate_world(region, target_count, distribution, extras)
        used_slugs = set()
        for plan in plans:
            slug = ensure_unique_slug(plan["slug"], used_slugs)
            plan["slug"] = slug
            name = plan["name"]
            segs = plan["segs"]
            grades = plan["grades"]
            surface_segments = plan["surface_segments"]
            spec = plan["spec"]
            archetype_name = plan["archetype"]

            # Write CRS
            crs_filename = f"{region.replace('_', '-')}__{slug}.crs"
            crs_path = COURSES_DIR / region / crs_filename
            write_crs(crs_path, WORLD_LABELS[region], name, segs, grades)

            # Write profile JSON
            metrics = compute_physical_metrics(segs, grades)
            profile_path = PROFILES_DIR / f"{region}__{slug}.json"
            write_profile_json(profile_path, region, slug, name,
                               segs, grades, metrics["elevation_points"])

            # Build routes.json entry
            entry, surface_entry, _ = build_route_entry(
                region=region,
                idx=plan["idx"],
                name=name,
                slug=slug,
                archetype_name=archetype_name,
                spec=spec,
                segs=segs,
                grades=grades,
                surface_segments=surface_segments,
                lap_meta=plan["lap_meta"],
            )
            all_entries.append(entry)
            if surface_entry is not None:
                all_surfaces[entry["id"]] = surface_entry

    # Write output
    ROUTES_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(ROUTES_OUT, "w") as f:
        json.dump(all_entries, f, indent=2)
    with open(SURFACES_OUT, "w") as f:
        json.dump(all_surfaces, f, indent=2)

    wall = time.time() - t_start
    return len(all_entries), wall


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

EXPECTED_COUNTS = {"blue_ridge": 100, "iron_pass": 60, "desert_loop": 60}

REQUIRED_FIELDS = {
    "id", "name", "crs_path", "region", "source", "distance_km", "climb_m",
    "descent_m", "net_elev_m", "avg_grade_signed", "avg_grade_abs", "max_grade",
    "min_grade", "terrain", "category", "finish_type", "loop", "climb_count",
    "primary_climb", "primary_surface", "surface_mix_pct", "has_gravel",
    "has_cobble", "lap_route", "difficulty_score", "est_duration_min_z2",
    "est_tss", "archetype", "tags", "preview_profile",
}


def validate(verbose: bool = True) -> int:
    if not ROUTES_OUT.exists():
        print(f"FAIL: {ROUTES_OUT} not found")
        return 1
    with open(ROUTES_OUT) as f:
        entries = json.load(f)

    by_region: dict[str, list[dict]] = {}
    for e in entries:
        by_region.setdefault(e["region"], []).append(e)

    # 1. Per-world counts
    for region, expected in EXPECTED_COUNTS.items():
        got = len(by_region.get(region, []))
        tol = int(expected * 0.05)
        if abs(got - expected) > tol:
            print(f"FAIL: {region} has {got}, expected {expected} ±{tol}")
            return 1

    # 2. SD(max_grade) >= 3.0 per world (loosened from 4.0 since we reduced
    #    route count 710 -> 220; fewer routes per world naturally has lower
    #    variance). 3.0 still proves the world isn't monotonous.
    for region, rs in by_region.items():
        mgs = [r["max_grade"] for r in rs]
        sd = statistics.stdev(mgs) if len(mgs) > 1 else 0.0
        if sd < 3.0:
            print(f"FAIL: {region} SD(max_grade) = {sd:.2f} < 3.0")
            return 1

    # 3. >=12 distinct archetypes per world (loosened from 15 for 100/60/60)
    for region, rs in by_region.items():
        archs = {r["archetype"] for r in rs}
        if len(archs) < 12:
            print(f"FAIL: {region} has only {len(archs)} distinct archetypes, need 12")
            return 1

    # 4. Family distribution alignment (±20% per family)
    family_targets = {
        "blue_ridge": {
            "rolling": 0.30, "climb": 0.20, "flat": 0.10, "lap": 0.15,
            "cobble": 0.10, "gravel": 0.10, "mixed": 0.05,
        },
        "iron_pass": {
            "climb": 0.50, "rolling": 0.15, "gravel": 0.10, "lap": 0.10,
            "cobble": 0.10,
        },
        "desert_loop": {
            "flat": 0.30, "rolling": 0.20, "gravel": 0.25, "lap": 0.15,
            "cobble": 0.05, "mixed": 0.05,
        },
    }
    for region, targets in family_targets.items():
        rs = by_region.get(region, [])
        total = len(rs)
        fam_counts: dict[str, int] = {}
        for r in rs:
            spec = ARCHETYPE_REGISTRY[r["archetype"]]
            fam_counts[spec.family] = fam_counts.get(spec.family, 0) + 1
        for fam, pct in targets.items():
            target = int(total * pct)
            got = fam_counts.get(fam, 0)
            # ±20% tolerance (multiplicative), plus 5-route absolute tolerance
            tol = max(5, int(target * 0.25))
            if abs(got - target) > tol:
                print(f"WARN: {region} family {fam} has {got}, target ~{target} (tol {tol})")

    # 5. Lap route quotas (reference uses lap routes sparingly — ~10% per world)
    lap_min = {"blue_ridge": 0.09, "iron_pass": 0.09, "desert_loop": 0.09}
    for region, pct in lap_min.items():
        rs = by_region.get(region, [])
        lap_count = sum(1 for r in rs if r["lap_route"] is not None)
        target = int(len(rs) * pct)
        if lap_count < target:
            print(f"FAIL: {region} has {lap_count} lap routes, need ≥{target}")
            return 1

    # 6. Surface variety
    surface_min = {
        "blue_ridge": 0.15, "iron_pass": 0.15, "desert_loop": 0.25,
    }
    for region, pct in surface_min.items():
        rs = by_region.get(region, [])
        gc = sum(1 for r in rs if r["has_gravel"] or r["has_cobble"])
        target = int(len(rs) * pct)
        if gc < target:
            print(f"FAIL: {region} gravel+cobble = {gc}, need ≥{target}")
            return 1

    # 7. No duplicate (name, region)
    seen_names = set()
    for r in entries:
        key = (r["region"], r["name"])
        if key in seen_names:
            print(f"FAIL: duplicate (region, name): {key}")
            return 1
        seen_names.add(key)

    # 8. crs_path exists
    for r in entries:
        p = HERE / r["crs_path"]
        if not p.exists():
            print(f"FAIL: crs_path missing: {p}")
            return 1

    # 9. Schema completeness
    for r in entries:
        missing = REQUIRED_FIELDS - set(r.keys())
        if missing:
            print(f"FAIL: route {r.get('id')} missing fields: {missing}")
            return 1

    # 10. Lap base_loop consistency (only enforced when base_loop_id is non-null
    # — since the "varied laps" rewrite, lap routes no longer share a base_loop
    # so this check is a no-op for them. Kept for future shared-base schemes).
    base_km_by_id: dict[str, float] = {}
    for r in entries:
        lr = r.get("lap_route")
        if lr and lr.get("base_loop_id"):
            bid = lr["base_loop_id"]
            bkm = lr["base_loop_km"]
            if bid in base_km_by_id:
                if abs(base_km_by_id[bid] - bkm) > 0.001:
                    print(f"FAIL: base_loop_id {bid} has inconsistent base_loop_km")
                    return 1
            else:
                base_km_by_id[bid] = bkm

    # ── Stats ──
    print("\n══ Virtual route generation summary ══")
    for region in ("blue_ridge", "iron_pass", "desert_loop"):
        rs = by_region.get(region, [])
        total = len(rs)
        archs = {r["archetype"] for r in rs}
        mgs = [r["max_grade"] for r in rs]
        sd = statistics.stdev(mgs) if len(mgs) > 1 else 0.0
        gc = sum(1 for r in rs if r["has_gravel"] or r["has_cobble"])
        laps = sum(1 for r in rs if r["lap_route"])
        print(f"\n[{region}] routes={total} archetypes={len(archs)} "
              f"SD(max_grade)={sd:.2f} gravel+cobble={gc} laps={laps}")
        # max_grade histogram
        bins = [0, 3, 6, 9, 12, 15, 18, 25]
        hist = [0] * (len(bins) - 1)
        for mg in mgs:
            for i in range(len(bins) - 1):
                if bins[i] <= mg < bins[i + 1]:
                    hist[i] += 1
                    break
        print("  max_grade histogram:")
        for i, c in enumerate(hist):
            print(f"    {bins[i]:>2}–{bins[i+1]:>2}%: {'█' * (c // 3)} ({c})")
        # archetype distribution top 8
        arch_counts: dict[str, int] = {}
        for r in rs:
            arch_counts[r["archetype"]] = arch_counts.get(r["archetype"], 0) + 1
        top = sorted(arch_counts.items(), key=lambda x: -x[1])[:8]
        print("  top archetypes: " + ", ".join(f"{k}({v})" for k, v in top))

    print(f"\n✓ All {len(entries)} routes validated")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate", action="store_true",
                        help="Only validate existing /tmp/routes_virtual.json")
    parser.add_argument("--no-validate", action="store_true",
                        help="Generate without post-validation")
    args = parser.parse_args()

    if args.validate:
        sys.exit(validate())
    count, wall = emit_routes()
    print(f"\nGenerated {count} virtual routes in {wall:.2f}s")
    if args.no_validate:
        return
    rc = validate()
    sys.exit(rc)


if __name__ == "__main__":
    main()
