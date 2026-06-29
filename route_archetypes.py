"""Route archetype library for Domestique (v3 — Section-based).

Rewritten to match real-world .crs climb feel:

- Universal 50 m (0.050 km) segment size.
- Section-based composition: every archetype assembles a list of
  ``Section`` objects (flat, false_flat_up, short_hill, kicker_up,
  gradual_climb, sustained_climb, steep_wall, rolling, descent,
  plateau) and the final profile is synthesised by
  ``build_route_from_sections``.
- Each archetype exposes **4-6 alternative composition templates**
  chosen by seed so the resulting 220 virtual routes do not collapse
  to one canonical shape per archetype.
- Per-section shape rules follow ``/tmp/climb_feel_spec.md`` (Gavia
  plateaus for HC, Box-Hill rhythmic texture for short city hills,
  Strade-Bianche long flats + episodic kickers for gravel, etc.).

Public API (used by ``generate_procedural_routes.py``):
    ARCHETYPE_REGISTRY  — dict[name -> ArchetypeSpec]
    ArchetypeOutput     — dataclass with segs, grades, surface, ...
    apply_smoothing / apply_clipping
    build_route_from_sections
    Section / SECTION_KINDS
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Callable, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SEG_KM = 0.050  # universal 50 m segments — matches every real-world .crs

SECTION_KINDS = (
    "flat",
    "false_flat_up",
    "false_flat_down",
    "short_hill",
    "kicker_up",
    "gradual_climb",
    "sustained_climb",
    "steep_wall",
    "rolling",
    "descent",
    "plateau",
)


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic hash-based RNG + 1D Perlin-style noise
# ─────────────────────────────────────────────────────────────────────────────

def _hash32(n: int, seed: int) -> int:
    x = (n * 2654435761 + seed * 1597334677) & 0xFFFFFFFF
    x ^= (x >> 16) & 0xFFFFFFFF
    x = (x * 0x7feb352d) & 0xFFFFFFFF
    x ^= (x >> 15) & 0xFFFFFFFF
    x = (x * 0x846ca68b) & 0xFFFFFFFF
    x ^= (x >> 16) & 0xFFFFFFFF
    return x


def seeded_random(seed: int, idx: int) -> float:
    return _hash32(idx, seed) / 0xFFFFFFFF


def _smoothstep(t: float) -> float:
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return t * t * (3.0 - 2.0 * t)


def _value_noise_1d(x: float, seed: int) -> float:
    xi = math.floor(x)
    xf = x - xi
    v0 = seeded_random(seed, int(xi)) * 2 - 1
    v1 = seeded_random(seed, int(xi) + 1) * 2 - 1
    t = _smoothstep(xf)
    return v0 + (v1 - v0) * t


def perlin_1d(x: float, seed: int, octaves: int = 4, persistence: float = 0.5) -> float:
    total = 0.0
    amp = 1.0
    freq = 1.0
    max_val = 0.0
    for o in range(octaves):
        total += _value_noise_1d(x * freq, seed + o * 1013) * amp
        max_val += amp
        amp *= persistence
        freq *= 2.0
    return total / max_val if max_val > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Section:
    kind: str
    length_km: float
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ArchetypeOutput:
    segs: list[float]
    grades: list[float]
    surface_segments: list[dict]
    terrain: str
    finish_type: str
    climb_count: int
    primary_climb: Optional[dict]
    template_id: str = ""  # which composition template was picked


@dataclass(frozen=True)
class ArchetypeSpec:
    name: str
    fn: Callable[[float, int], ArchetypeOutput]
    dist_min_km: float
    dist_max_km: float
    smoothing_max_change: float
    max_grade_cap: float
    min_grade_floor: float
    family: str
    short_description: str


# ─────────────────────────────────────────────────────────────────────────────
# Surface helpers
# ─────────────────────────────────────────────────────────────────────────────

def uniform_surface(total_km: float, surface: str) -> list[dict]:
    return [{"start_km": 0.0, "end_km": round(total_km, 4), "surface": surface}]


def mixed_surface_segments(total_km: float, seed: int,
                           pattern: list[tuple[float, float, str]]) -> list[dict]:
    out = []
    for (s_pct, e_pct, surf) in pattern:
        out.append({
            "start_km": round(s_pct * total_km, 4),
            "end_km": round(e_pct * total_km, 4),
            "surface": surf,
        })
    return out


def scatter_cobble_sectors(total_km: float, seed: int, sector_count: int,
                           sector_min_km: float, sector_max_km: float,
                           region: tuple[float, float] = (0.05, 0.95)) -> list[dict]:
    r_start, r_end = region
    usable_km = (r_end - r_start) * total_km
    sectors = []
    for i in range(sector_count):
        r = seeded_random(seed, i + 3000)
        length = sector_min_km + r * (sector_max_km - sector_min_km)
        sectors.append(length)
    total_sector_km = sum(sectors)
    if total_sector_km > usable_km * 0.6:
        scale = (usable_km * 0.5) / total_sector_km
        sectors = [s * scale for s in sectors]
        total_sector_km = sum(sectors)
    asphalt_gap_km = (usable_km - total_sector_km) / (sector_count + 1)

    segs: list[dict] = []
    cursor = 0.0
    if r_start > 0:
        segs.append({"start_km": 0.0, "end_km": round(r_start * total_km, 4), "surface": "asphalt"})
        cursor = r_start * total_km
    for s_len in sectors:
        gap_end = cursor + asphalt_gap_km
        segs.append({"start_km": round(cursor, 4), "end_km": round(gap_end, 4), "surface": "asphalt"})
        cursor = gap_end
        segs.append({"start_km": round(cursor, 4), "end_km": round(cursor + s_len, 4), "surface": "cobble"})
        cursor += s_len
    segs.append({"start_km": round(cursor, 4), "end_km": round(total_km, 4), "surface": "asphalt"})
    segs = [s for s in segs if s["end_km"] - s["start_km"] > 0.001]
    if segs:
        segs[0]["start_km"] = 0.0
        segs[-1]["end_km"] = round(total_km, 4)
    return segs


# ─────────────────────────────────────────────────────────────────────────────
# Aperiodic primitives — fBm, Poisson placement, asymmetric hills, Markov climb
#
# Philosophy: real road elevation has 1/f^β (β ∈ [1.2, 1.8]) power spectrum and
# autocorrelation < 0.4 at meaningful lags. We achieve this by layering:
#   * Fractional Brownian motion (fBm) for base texture
#   * Poisson-placed features for aperiodic hills
#   * Asymmetric Gaussian for per-hill shape
#   * Markov chain on grade state for climb macro-structure
# No sine/cosine is used anywhere — the resulting profiles are intrinsically
# aperiodic.
# ─────────────────────────────────────────────────────────────────────────────

def _fbm_1d(x: float, seed: int, *, octaves: int = 6, lacunarity: float = 2.0,
            persistence: float = 0.55) -> float:
    """Fractional Brownian motion at position ``x``.

    Sum of value-noise (``perlin_1d``) at increasing frequencies with
    geometrically decreasing amplitude. Output is normalised to roughly
    [-1, 1]. Produces natural 1/f-like terrain feel (no periodicity).
    """
    total = 0.0
    amp = 1.0
    freq = 1.0
    max_amp = 0.0
    for o in range(octaves):
        # perlin_1d here is our 1D value-noise with multiple octaves; feed it
        # with a single-octave sample per layer by setting octaves=1 and
        # varying our own frequency.
        total += _value_noise_1d(x * freq, seed + o * 1013) * amp
        max_amp += amp
        freq *= lacunarity
        amp *= persistence
    return total / max_amp if max_amp else 0.0


def _poisson_positions(length_km: float, mean_spacing_km: float, seed: int
                       ) -> list[float]:
    """Positions in [0, length_km] with exponentially-distributed gaps.

    Exponential inter-arrival times ⇒ a Poisson point process on the
    interval. By construction there is no fixed period, so the resulting
    feature placement is aperiodic.
    """
    positions: list[float] = []
    if length_km <= 0 or mean_spacing_km <= 0:
        return positions
    t = 0.0
    i = 0
    # hard cap to avoid pathological loops if RNG gives near-1 values
    max_features = int(length_km / max(0.02, mean_spacing_km * 0.1)) + 8
    while i < max_features:
        u = seeded_random(seed, i + 12345)
        # Inverse CDF of Exponential(1/mean): gap = -mean * ln(1-U)
        gap = -mean_spacing_km * math.log(max(1e-6, 1.0 - u))
        t += gap
        if t >= length_km:
            break
        positions.append(t)
        i += 1
    return positions


def _asymmetric_hill(t: float, center: float, rise_sigma: float,
                     fall_sigma: float, peak: float) -> float:
    """Gaussian bell peaked at ``center`` with different rise/fall widths.

    * rise_sigma < fall_sigma → steep up, gentle down
    * rise_sigma > fall_sigma → gentle up, steep down (punchy summit)
    """
    dt = t - center
    if dt <= 0:
        rs = max(1e-6, rise_sigma)
        return peak * math.exp(-(dt / rs) ** 2)
    fs = max(1e-6, fall_sigma)
    return peak * math.exp(-(dt / fs) ** 2)


# Markov-chain states for a climb's grade trajectory. Each state carries an
# expected grade and a typical segment duration. Transitions are hand-tuned
# to mimic real-climb cadence: hard→relief→hard is common, hard→flat is not.
_CLIMB_GRADE_STATES = {
    "flat":   {"grade_mu": 1.0,  "grade_sd": 0.5, "duration_km_mu": 0.5},
    "tempo":  {"grade_mu": 5.5,  "grade_sd": 0.8, "duration_km_mu": 1.2},
    "hard":   {"grade_mu": 8.0,  "grade_sd": 1.0, "duration_km_mu": 1.5},
    "steep":  {"grade_mu": 11.0, "grade_sd": 1.5, "duration_km_mu": 0.6},
    "relief": {"grade_mu": 3.0,  "grade_sd": 1.0, "duration_km_mu": 0.4},
}

_CLIMB_TRANSITIONS = {
    "flat":   {"tempo": 0.5,  "hard": 0.3,   "relief": 0.2},
    "tempo":  {"hard": 0.45,  "steep": 0.15, "relief": 0.2,  "tempo": 0.2},
    "hard":   {"steep": 0.3,  "hard": 0.25,  "relief": 0.3,  "tempo": 0.15},
    "steep":  {"hard": 0.5,   "relief": 0.35,"steep": 0.15},
    "relief": {"tempo": 0.4,  "hard": 0.35,  "relief": 0.1,  "steep": 0.15},
}

# Bias variant that stays in the hot states for steep_wall kind
_CLIMB_TRANSITIONS_STEEP = {
    "flat":   {"hard": 0.5,   "steep": 0.35, "tempo": 0.15},
    "tempo":  {"hard": 0.55,  "steep": 0.3,  "tempo": 0.15},
    "hard":   {"steep": 0.55, "hard": 0.25,  "relief": 0.1,  "tempo": 0.10},
    "steep":  {"steep": 0.45, "hard": 0.4,   "relief": 0.15},
    "relief": {"hard": 0.55,  "steep": 0.3,  "tempo": 0.15},
}


def _markov_climb(length_km: float, seed: int, *, start_state: str = "tempo",
                  transitions: dict = None, avg_grade: float = None,
                  ) -> list[float]:
    """Generate a climb grade profile via a state machine.

    Returns one grade value per SEG_KM (50 m) segment. The output is
    intrinsically aperiodic: state durations are stochastic and transitions
    are probabilistic, so no repeating cycle emerges.

    If ``avg_grade`` is supplied, state grade means are shifted so the mean
    of the returned series is close to ``avg_grade`` while preserving the
    relative state structure.
    """
    if transitions is None:
        transitions = _CLIMB_TRANSITIONS
    n_segs = max(1, round(length_km / SEG_KM))
    out: list[float] = []
    state = start_state if start_state in _CLIMB_GRADE_STATES else "tempo"
    seg_idx = 0
    step = 0
    while seg_idx < n_segs:
        info = _CLIMB_GRADE_STATES[state]
        jitter = 0.6 + 1.2 * seeded_random(seed, seg_idx + 7777)
        dur_km = max(0.1, info["duration_km_mu"] * jitter)
        dur_segs = max(1, min(n_segs - seg_idx, round(dur_km / SEG_KM)))
        base_grade = info["grade_mu"]
        sd = info["grade_sd"]
        for i in range(dur_segs):
            # Per-segment fBm jitter around the state mean (NOT sinusoidal)
            noise = _fbm_1d(seg_idx * 0.12, seed + 333, octaves=3,
                            persistence=0.5) * sd
            out.append(base_grade + noise)
            seg_idx += 1
            if seg_idx >= n_segs:
                break
        # Probabilistic transition
        u = seeded_random(seed, seg_idx + 99999 + step * 131)
        cum = 0.0
        next_state = state
        for s, p in transitions.get(state, {state: 1.0}).items():
            cum += p
            if u < cum:
                next_state = s
                break
        state = next_state
        step += 1
    # Optional global shift to match requested average
    if avg_grade is not None and out:
        actual = sum(out) / len(out)
        shift = avg_grade - actual
        out = [g + shift for g in out]
    return out


def _layered_rolling(length_km: float, seed: int, *,
                     baseline_grade: float, amp_macro: float,
                     amp_micro: float, feature_mean_spacing: float,
                     feature_peak_range: tuple) -> list[float]:
    """Rolling terrain = fBm base + Poisson-placed asymmetric hills + chop.

    Intrinsically aperiodic — no sine/cos anywhere.
    """
    n_segs = max(1, round(length_km / SEG_KM))
    # Macro fBm layer: gentle undulation. Use per-segment freq ~0.4 so
    # consecutive samples are decorrelated enough to look like terrain.
    base = [
        baseline_grade + _fbm_1d(i * 0.4, seed, octaves=4, persistence=0.55) * amp_macro
        for i in range(n_segs)
    ]
    # Aperiodic hills via Poisson placement
    positions = _poisson_positions(length_km, feature_mean_spacing, seed + 1111)
    peak_lo, peak_hi = feature_peak_range
    for p_idx, pos in enumerate(positions):
        r1 = seeded_random(seed, p_idx + 4000)
        r2 = seeded_random(seed, p_idx + 5000)
        r3 = seeded_random(seed, p_idx + 6000)
        r4 = seeded_random(seed, p_idx + 6500)
        peak = peak_lo + r1 * (peak_hi - peak_lo)
        # Randomly pick asymmetry direction (some hills punchy, some gentle)
        if r4 < 0.5:
            rise_sigma = 0.12 + r2 * 0.15  # km — fairly steep up
            fall_sigma = 0.18 + r3 * 0.30  # km — gentler down
        else:
            rise_sigma = 0.18 + r2 * 0.28  # gentle up
            fall_sigma = 0.10 + r3 * 0.15  # punchy down
        # Optional dip: with 25% probability, invert the hill (becomes a dip)
        if seeded_random(seed, p_idx + 7000) < 0.25:
            peak = -peak * 0.7
        for i in range(n_segs):
            t_km = i * SEG_KM
            # Only evaluate nearby samples for efficiency
            if abs(t_km - pos) > 4.0 * max(rise_sigma, fall_sigma):
                continue
            base[i] += _asymmetric_hill(t_km, pos, rise_sigma, fall_sigma, peak)
    # Micro-noise layer (high-freq chop) — aggressively high frequency to
    # decorrelate adjacent samples.
    if amp_micro > 0:
        for i in range(n_segs):
            base[i] += _fbm_1d(i * 1.9, seed + 77, octaves=2,
                               persistence=0.5) * amp_micro
    return base


# ─────────────────────────────────────────────────────────────────────────────
# Section-shape functions
#
# Each function has signature `fn(sec, n, seed) -> list[float]` and returns
# one grade per SEG_KM (50 m) segment, producing exactly ``n`` values.
# Backward-compatible with archetype composition code — parameter names on
# Section.params are unchanged (grade, avg, peak, amp, roll_amp, baseline,
# period_km). ``period_km`` is now interpreted as a loose feature-spacing
# hint rather than a sinusoidal period.
# ─────────────────────────────────────────────────────────────────────────────

def _shape_flat(sec: Section, n: int, seed: int) -> list[float]:
    """Flat = baseline + tiny fBm chop. Target SD < 0.8.

    Uses per-segment frequency 0.4 (so each 50 m step advances the noise
    input ~0.4 lattice units) plus a decorrelating micro layer. Without
    the micro layer, successive value-noise samples are too correlated
    and the profile looks like a smooth curve.
    """
    base = float(sec.params.get("grade", 0.0))
    out: list[float] = []
    for i in range(n):
        # Macro-ish fBm — gentle drift
        macro = _fbm_1d(i * 0.35, seed, octaves=3, persistence=0.45) * 0.30
        # High-frequency chop — decorrelates consecutive samples
        micro = _fbm_1d(i * 1.7, seed + 7919, octaves=2, persistence=0.4) * 0.25
        out.append(base + macro + micro)
    return out


def _shape_false_flat_up(sec: Section, n: int, seed: int) -> list[float]:
    """False-flat up: baseline drag + fBm drift + micro chop."""
    base = float(sec.params.get("grade", 2.0))
    out: list[float] = []
    for i in range(n):
        macro = _fbm_1d(i * 0.35, seed, octaves=3, persistence=0.5) * 0.40
        micro = _fbm_1d(i * 1.9, seed + 211, octaves=2, persistence=0.45) * 0.30
        out.append(base + macro + micro)
    return out


def _shape_false_flat_down(sec: Section, n: int, seed: int) -> list[float]:
    """Mirror of false_flat_up, negative baseline."""
    base = float(sec.params.get("grade", -2.0))
    out: list[float] = []
    for i in range(n):
        macro = _fbm_1d(i * 0.35, seed, octaves=3, persistence=0.5) * 0.40
        micro = _fbm_1d(i * 1.9, seed + 211, octaves=2, persistence=0.45) * 0.30
        out.append(base + macro + micro)
    return out


def _shape_short_hill(sec: Section, n: int, seed: int) -> list[float]:
    """One asymmetric hill peaked near mid-section + fBm chop."""
    peak = float(sec.params.get("peak", 6.0))
    length_km = n * SEG_KM
    # Randomise centre, rise/fall sigmas per seed — NO fixed Gaussian.
    r_ctr = seeded_random(seed, 101)
    r_rise = seeded_random(seed, 102)
    r_fall = seeded_random(seed, 103)
    r_dir = seeded_random(seed, 104)
    center = length_km * (0.35 + 0.30 * r_ctr)
    # Ratio: 0.5..2.0 asymmetry in either direction
    if r_dir < 0.5:
        rise_sigma = length_km * (0.12 + 0.08 * r_rise)
        fall_sigma = length_km * (0.18 + 0.14 * r_fall)
    else:
        rise_sigma = length_km * (0.18 + 0.14 * r_rise)
        fall_sigma = length_km * (0.10 + 0.08 * r_fall)
    out: list[float] = []
    for i in range(n):
        t_km = i * SEG_KM
        bell = _asymmetric_hill(t_km, center, rise_sigma, fall_sigma, peak)
        macro = _fbm_1d(i * 0.35, seed + 55, octaves=2, persistence=0.5) * 0.35
        micro = _fbm_1d(i * 1.8, seed + 155, octaves=2, persistence=0.4) * 0.35
        out.append(bell + macro + micro)
    return out


def _shape_kicker_up(sec: Section, n: int, seed: int) -> list[float]:
    """Smoothstep sigmoid ramp that stays UP at the end + fBm chop."""
    peak = float(sec.params.get("peak", 8.0))
    # Randomise the inflection point for variety
    r_infl = seeded_random(seed, 201)
    inflection = 0.5 + 0.2 * r_infl  # 0.5..0.7
    out: list[float] = []
    for i in range(n):
        t = (i + 0.5) / n
        ramp = _smoothstep(min(1.0, t / inflection))
        macro = _fbm_1d(i * 0.35, seed, octaves=2, persistence=0.5) * 0.25
        micro = _fbm_1d(i * 1.8, seed + 300, octaves=2, persistence=0.4) * 0.30
        out.append(peak * ramp + macro + micro)
    return out


def _shape_gradual_climb(sec: Section, n: int, seed: int) -> list[float]:
    """Markov climb starting in 'tempo' + smoothstep ease at edges."""
    avg = float(sec.params.get("avg", 6.0))
    roll_amp = float(sec.params.get("roll_amp", 1.3))
    length_km = n * SEG_KM
    grades = _markov_climb(length_km, seed, start_state="tempo",
                           avg_grade=avg)
    # Trim / pad to exactly n
    if len(grades) < n:
        grades = grades + [grades[-1] if grades else avg] * (n - len(grades))
    grades = grades[:n]
    # Add fBm micro-layer scaled by roll_amp so low roll_amp → calmer.
    # High-frequency chop (1.5/seg) decorrelates consecutive samples.
    for i in range(n):
        grades[i] += _fbm_1d(i * 1.5, seed + 99, octaves=2) * (roll_amp * 0.35)
    # Ease in/out at section edges so composition blends naturally
    ease_n = max(2, min(n // 8, 16))
    first = grades[0]
    for i in range(ease_n):
        w = _smoothstep((i + 1) / ease_n)
        grades[i] = first * (1 - w) + grades[i] * w
    return grades


def _shape_sustained_climb(sec: Section, n: int, seed: int) -> list[float]:
    """Markov climb starting 'hard' — lots of state changes."""
    avg = float(sec.params.get("avg", 8.0))
    roll_amp = float(sec.params.get("roll_amp", 1.8))
    length_km = n * SEG_KM
    grades = _markov_climb(length_km, seed, start_state="hard",
                           avg_grade=avg)
    if len(grades) < n:
        grades = grades + [grades[-1] if grades else avg] * (n - len(grades))
    grades = grades[:n]
    for i in range(n):
        grades[i] += _fbm_1d(i * 1.6, seed + 777, octaves=3,
                             persistence=0.55) * (roll_amp * 0.4)
    return grades


def _shape_steep_wall(sec: Section, n: int, seed: int) -> list[float]:
    """Markov climb biased into steep/hard states."""
    avg = float(sec.params.get("avg", 13.0))
    length_km = n * SEG_KM
    grades = _markov_climb(length_km, seed, start_state="steep",
                           transitions=_CLIMB_TRANSITIONS_STEEP,
                           avg_grade=avg)
    if len(grades) < n:
        grades = grades + [grades[-1] if grades else avg] * (n - len(grades))
    grades = grades[:n]
    # Extra chop so the wall feels textured
    for i in range(n):
        grades[i] += _fbm_1d(i * 2.0, seed + 1234, octaves=2) * 1.3
    return grades


def _shape_rolling(sec: Section, n: int, seed: int) -> list[float]:
    """fBm base + Poisson-placed asymmetric hills + micro chop."""
    baseline = float(sec.params.get("baseline", 0.0))
    amp = float(sec.params.get("amp", 2.5))
    # Feature spacing derived from any legacy period_km hint, else sensible default
    period_km = float(sec.params.get("period_km", 1.2))
    mean_spacing = max(0.3, period_km)
    length_km = n * SEG_KM
    # Peak range scaled to amp for backward-compatible amplitude feel
    peak_lo = amp * 0.6
    peak_hi = amp * 1.8
    grades = _layered_rolling(
        length_km, seed,
        baseline_grade=baseline,
        amp_macro=amp * 0.5,
        amp_micro=amp * 0.25,
        feature_mean_spacing=mean_spacing,
        feature_peak_range=(peak_lo, peak_hi),
    )
    if len(grades) < n:
        grades = grades + [grades[-1] if grades else baseline] * (n - len(grades))
    return grades[:n]


def _shape_descent(sec: Section, n: int, seed: int) -> list[float]:
    """Mirror of gradual_climb: negative-grade Markov with ease at edges."""
    grade = float(sec.params.get("grade", -5.0))
    length_km = n * SEG_KM
    # Use the Markov machine negated — flip signs of grade means via avg_grade
    pos_grades = _markov_climb(length_km, seed, start_state="tempo",
                               avg_grade=abs(grade))
    if len(pos_grades) < n:
        pos_grades = pos_grades + [pos_grades[-1] if pos_grades else abs(grade)] * (n - len(pos_grades))
    pos_grades = pos_grades[:n]
    grades = [-g for g in pos_grades]
    # Smoothstep ease in/out at edges of the descent
    ease_n = max(2, min(n // 8, 16))
    first = grades[0]
    last = grades[-1]
    for i in range(ease_n):
        w = _smoothstep((i + 1) / ease_n)
        grades[i] = first * (1 - w) + grades[i] * w
    for j in range(ease_n):
        idx = n - 1 - j
        w = _smoothstep((j + 1) / ease_n)
        grades[idx] = last * (1 - w) + grades[idx] * w
    return grades


def _shape_plateau(sec: Section, n: int, seed: int) -> list[float]:
    """High-altitude plateau: small amp fBm + sparse tiny features."""
    baseline = float(sec.params.get("grade", 3.0))
    length_km = n * SEG_KM
    grades = _layered_rolling(
        length_km, seed,
        baseline_grade=baseline,
        amp_macro=0.8,
        amp_micro=0.3,
        feature_mean_spacing=2.0,
        feature_peak_range=(1.0, 2.5),
    )
    if len(grades) < n:
        grades = grades + [grades[-1] if grades else baseline] * (n - len(grades))
    return grades[:n]


_SHAPE_FNS = {
    "flat": _shape_flat,
    "false_flat_up": _shape_false_flat_up,
    "false_flat_down": _shape_false_flat_down,
    "short_hill": _shape_short_hill,
    "kicker_up": _shape_kicker_up,
    "gradual_climb": _shape_gradual_climb,
    "sustained_climb": _shape_sustained_climb,
    "steep_wall": _shape_steep_wall,
    "rolling": _shape_rolling,
    "descent": _shape_descent,
    "plateau": _shape_plateau,
}


def _shape_section(sec: Section, n: int, seed: int) -> list[float]:
    fn = _SHAPE_FNS.get(sec.kind)
    if fn is None:
        raise ValueError(f"Unknown section kind: {sec.kind}")
    return fn(sec, n, seed)


# ─────────────────────────────────────────────────────────────────────────────
# Compose sections into full profile
# ─────────────────────────────────────────────────────────────────────────────

MIN_SECTION_KM = 0.5  # Sections shorter than this are merged before
                      # inflation; avoids a 0.01 km section being rounded
                      # up to 0.10 km (2 * SEG_KM) by the min-n=2 clamp.

# Grade-bearing keys in each section's ``params``. Used when merging a tiny
# section into its neighbor so the merged section keeps a length-weighted
# average gradient rather than just absorbing length silently.
_GRADE_PARAM_KEYS = ("grade", "avg", "baseline")


def _merge_tiny_sections(sections: list[Section],
                         min_km: float = MIN_SECTION_KM) -> list[Section]:
    """Merge sections shorter than ``min_km`` into an adjacent neighbor.

    The merged section keeps the *neighbor's* kind/params (since ``kind`` is
    nominal and can't be averaged), but its ``length_km`` is the sum of both
    sections and any grade-bearing param is the length-weighted mean.
    """
    if not sections:
        return sections
    # First drop zero/negative-length junk so we never merge into one.
    kept = [s for s in sections if s.length_km > 0]
    if not kept:
        return kept
    # Fast path: all sections already meet the minimum.
    if all(s.length_km >= min_km for s in kept):
        return kept

    def _merge_pair(keeper: Section, absorbed: Section) -> Section:
        new_len = keeper.length_km + absorbed.length_km
        new_params = dict(keeper.params)
        for key in _GRADE_PARAM_KEYS:
            if key in keeper.params and key in absorbed.params:
                kw = keeper.length_km
                aw = absorbed.length_km
                tot = kw + aw
                if tot > 0:
                    new_params[key] = (
                        float(keeper.params[key]) * kw
                        + float(absorbed.params[key]) * aw
                    ) / tot
        return Section(kind=keeper.kind, length_km=new_len, params=new_params)

    # Walk forward; whenever we see a tiny section, merge it into its
    # neighbor (prefer the previous one so the kind is sticky). If it's
    # the very first section, merge it into the next one instead.
    out: list[Section] = []
    i = 0
    while i < len(kept):
        sec = kept[i]
        if sec.length_km < min_km:
            if out:
                out[-1] = _merge_pair(out[-1], sec)
                i += 1
                continue
            # No predecessor: fold into the next section if there is one.
            if i + 1 < len(kept):
                out.append(_merge_pair(kept[i + 1], sec))
                i += 2
                continue
            # Single tiny section with no neighbors — keep as-is; the n>=2
            # clamp below will still produce at least 0.1 km of output.
        out.append(sec)
        i += 1
    return out


def build_route_from_sections(sections: list[Section], seed: int
                              ) -> tuple[list[float], list[float]]:
    """Synthesise (segs, grades) from a list of Sections, 50 m each."""
    # Merge sections shorter than MIN_SECTION_KM before the min-n=2 inflation
    # step below would otherwise quietly 2-10x their on-profile length.
    sections = _merge_tiny_sections(sections)
    all_segs: list[float] = []
    all_grades: list[float] = []
    prev_end = 0.0
    for i, sec in enumerate(sections):
        if sec.length_km <= 0:
            continue
        n = max(2, int(round(sec.length_km / SEG_KM)))
        actual_length = n * SEG_KM
        shaped = _shape_section(sec, n, seed + i * 7919)
        # Smoothstep ease-in against previous section's end grade
        blend = max(2, min(n // 10, 8))
        for j in range(blend):
            t = (j + 1) / blend
            w = _smoothstep(t)
            shaped[j] = prev_end * (1 - w) + shaped[j] * w
        all_segs.extend([SEG_KM] * n)
        all_grades.extend(shaped)
        prev_end = shaped[-1] if shaped else prev_end
    return all_segs, all_grades


# ─────────────────────────────────────────────────────────────────────────────
# Post-processing helpers
# ─────────────────────────────────────────────────────────────────────────────

def apply_smoothing(grades: list[float], max_change: float) -> list[float]:
    if not grades:
        return grades
    out = list(grades)
    for i in range(1, len(out)):
        if out[i] - out[i - 1] > max_change:
            out[i] = out[i - 1] + max_change
        elif out[i - 1] - out[i] > max_change:
            out[i] = out[i - 1] - max_change
    for i in range(len(out) - 2, -1, -1):
        if out[i] - out[i + 1] > max_change:
            out[i] = out[i + 1] + max_change
        elif out[i + 1] - out[i] > max_change:
            out[i] = out[i + 1] - max_change
    return out


def apply_clipping(grades: list[float], lo: float, hi: float) -> list[float]:
    return [max(lo, min(hi, g)) for g in grades]


def segment_lengths(total_km: float, seed: int, seg_size_km: float = SEG_KM
                    ) -> list[float]:
    """Uniform 50 m segments (kept for backwards compat with legacy tests)."""
    n = max(2, int(round(total_km / seg_size_km)))
    return [seg_size_km] * n


def _detect_climbs(segs: list[float], grades: list[float],
                   min_grade: float = 5.0, min_len_km: float = 0.5) -> list[dict]:
    climbs: list[dict] = []
    cum = [0.0]
    for s in segs:
        cum.append(cum[-1] + s)
    # Tail-absorption policy: once a climb ends (grade drops below
    # ``min_grade``), we may still absorb the immediate downhill-into-flat
    # tail, but only while grade >= TAIL_MIN_GRADE *and* total tail length
    # is below TAIL_MAX_KM. This stops a 4% ramp for kilometres from being
    # folded into a 5% climb and diluting its avg_grade / cat points.
    TAIL_MIN_GRADE = 4.0
    TAIL_MAX_KM = 0.5
    i = 0
    while i < len(grades):
        if grades[i] >= min_grade:
            start = i
            # Main climb body: accumulate while at or above the real threshold.
            while i < len(grades) and grades[i] >= min_grade:
                i += 1
            # Bounded tail: absorb ramp-down sections >= TAIL_MIN_GRADE but
            # stop at TAIL_MAX_KM or when grade first dips below TAIL_MIN_GRADE.
            tail_start_km = cum[i]
            while (i < len(grades)
                   and grades[i] >= TAIL_MIN_GRADE
                   and grades[i] < min_grade
                   and (cum[i + 1] - tail_start_km) <= TAIL_MAX_KM):
                i += 1
            length = cum[i] - cum[start]
            if length >= min_len_km:
                sub_grades = grades[start:i]
                sub_segs = segs[start:i]
                total_d = sum(sub_segs)
                if total_d > 0:
                    avg_g = sum(g * d for g, d in zip(sub_grades, sub_segs)) / total_d
                else:
                    avg_g = 0.0
                climbs.append({
                    "start_km": round(cum[start], 3),
                    "length_km": round(length, 3),
                    "avg_grade": round(avg_g, 2),
                    "max_grade": round(max(sub_grades), 2),
                })
        else:
            i += 1
    return climbs


def _primary_climb_from(segs: list[float], grades: list[float]) -> Optional[dict]:
    climbs = _detect_climbs(segs, grades)
    if not climbs:
        return None
    return max(climbs, key=lambda c: c["length_km"])


# ─────────────────────────────────────────────────────────────────────────────
# Template pick helper
# ─────────────────────────────────────────────────────────────────────────────

def _pick_template(seed: int, n_templates: int, salt: int = 0) -> int:
    return _hash32(salt * 31 + 11, seed) % n_templates


# ─────────────────────────────────────────────────────────────────────────────
# Lead-in helper (30% of climbs get a 1-2 km false_flat_up lead-in)
# ─────────────────────────────────────────────────────────────────────────────

def _lead_in(seed: int, probability: float = 0.30) -> list[Section]:
    if seeded_random(seed, 50000) < probability:
        length = 1.0 + seeded_random(seed, 50001) * 1.0  # 1-2 km
        grade = 1.0 + seeded_random(seed, 50002) * 2.0  # 1-3%
        return [Section("false_flat_up", length, {"grade": grade})]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Jitter helpers for aperiodic section layouts
# ─────────────────────────────────────────────────────────────────────────────

def _jit(seed: int, salt: int, center: float, pct: float) -> float:
    """Seeded ±pct jitter around center (e.g. _jit(seed,1,1.0,0.2) -> 0.8..1.2)."""
    r = seeded_random(seed, salt)
    return center * (1.0 - pct + 2 * pct * r)


def _jit_abs(seed: int, salt: int, lo: float, hi: float) -> float:
    """Seeded uniform draw in [lo, hi]."""
    return lo + seeded_random(seed, salt) * (hi - lo)


def _split_poisson(total: float, n: int, seed: int, min_frac: float = 0.5) -> list[float]:
    """Split ``total`` into ``n`` positive aperiodic lengths (never all equal).

    Raw exponential draws normalised to sum ``total``. ``min_frac`` enforces
    every segment ≥ (total/n) * min_frac so we don't generate 0-length
    fragments when the RNG is unlucky.
    """
    if n <= 1:
        return [total]
    raw = [-math.log(max(1e-6, 1.0 - seeded_random(seed, i + 6100))) for i in range(n)]
    s = sum(raw) or 1.0
    parts = [r / s * total for r in raw]
    floor = (total / n) * min_frac
    # Clamp small parts up to floor, re-normalise the rest
    extra = sum(max(0.0, floor - p) for p in parts)
    parts = [max(floor, p) for p in parts]
    over = sum(parts) - total
    if over > 0:
        # Shave proportionally from the ones above floor
        slack = [max(0.0, p - floor) for p in parts]
        tot_slack = sum(slack) or 1.0
        parts = [p - over * (s / tot_slack) for p, s in zip(parts, slack)]
    return parts


def _scale_to(total_km: float, sections: list[Section]) -> list[Section]:
    """Re-scale a list of sections so their lengths sum to exactly total_km."""
    s = sum(sec.length_km for sec in sections)
    if s <= 0:
        return sections
    k = total_km / s
    return [Section(sec.kind, sec.length_km * k, sec.params) for sec in sections]


# ═════════════════════════════════════════════════════════════════════════════
# ARCHETYPE IMPLEMENTATIONS
# Each archetype returns (sections, extras) via an internal helper, then a
# wrapper runs build_route_from_sections + builds ArchetypeOutput.
# ═════════════════════════════════════════════════════════════════════════════


def _finalize(sections: list[Section], seed: int, total_km_hint: float,
              surface_segments: list[dict], terrain: str, finish_type: str,
              template_id: str, loop_primary: bool = True
              ) -> ArchetypeOutput:
    """Build route, compute primary_climb, wrap in ArchetypeOutput."""
    segs, grades = build_route_from_sections(sections, seed)
    if not segs:
        # defensive: produce a 1-segment flat
        segs, grades = [SEG_KM], [0.0]
    # Primary climb detection (best effort)
    primary_climb = _primary_climb_from(segs, grades)
    climb_count = len(_detect_climbs(segs, grades))
    if primary_climb is None and terrain == "climb":
        # fabricate a coarse primary_climb from whole-route metrics
        total_d = sum(segs)
        avg_g = sum(g * d for g, d in zip(grades, segs)) / max(0.001, total_d)
        if avg_g > 0.5:
            primary_climb = {
                "start_km": 0.0,
                "length_km": round(total_d, 2),
                "avg_grade": round(avg_g, 2),
                "max_grade": round(max(grades) if grades else 0.0, 2),
            }
    return ArchetypeOutput(
        segs=segs, grades=grades,
        surface_segments=surface_segments,
        terrain=terrain,
        finish_type=finish_type,
        climb_count=max(climb_count, 1 if primary_climb else 0),
        primary_climb=primary_climb,
        template_id=template_id,
    )


# ── FLAT FAMILY (4) ──────────────────────────────────────────────────────────
#
# All templates here map to reference/real-world signatures in
# reference doc FLAT family + MX_INNSBRUCKRING (for hill end).
# Section lengths use seeded jitter (_jit) — no "total_km / N" equal splits.

# --- flat_tt templates (map to FT_TEMPUS_FUGIT, FT_VOLCANO_FLAT, FT_TICK_TOCK,
#                         FT_BIG_FLAT_8, FT_MALL_TT, FT_LONDON_CLASSIQUE) ---

def _tpl_ft_tempus_fugit(total_km: float, seed: int) -> list[Section]:
    """FT_TEMPUS_FUGIT — pure straight-shot flat with barely-perceptible mesa bump."""
    bump_pos = _jit_abs(seed, 1, 0.35, 0.65)
    bump_w = _jit_abs(seed, 2, 0.8, 1.5)
    pre = total_km * bump_pos - bump_w * 0.5
    post = total_km - pre - bump_w
    return [
        Section("flat", pre, {"grade": _jit_abs(seed, 3, 0.1, 0.4)}),
        Section("false_flat_up", bump_w, {"grade": _jit_abs(seed, 4, 0.7, 1.1)}),
        Section("flat", post, {"grade": _jit_abs(seed, 5, -0.2, 0.2)}),
    ]


def _tpl_ft_volcano_flat(total_km: float, seed: int) -> list[Section]:
    """FT_VOLCANO_FLAT — continuous tiny rollers, no feature > 8 m."""
    return [Section("rolling", total_km,
                    {"baseline": _jit_abs(seed, 1, -0.1, 0.3),
                     "amp": _jit_abs(seed, 2, 0.3, 0.7),
                     "period_km": _jit_abs(seed, 3, 2.0, 4.0)})]


def _tpl_ft_tick_tock(total_km: float, seed: int) -> list[Section]:
    """FT_TICK_TOCK — slight positive drag throughout."""
    return [Section("flat", total_km, {"grade": _jit_abs(seed, 1, 0.1, 0.4)})]


def _tpl_ft_big_flat_8(total_km: float, seed: int) -> list[Section]:
    """FT_BIG_FLAT_8 — aperiodic 3-part split flat / false-flat / flat."""
    lens = _split_poisson(total_km, 3, seed + 401, min_frac=0.6)
    return [
        Section("flat", lens[0], {"grade": _jit_abs(seed, 1, -0.1, 0.2)}),
        Section("false_flat_up", lens[1], {"grade": _jit_abs(seed, 2, 0.4, 0.9)}),
        Section("flat", lens[2], {"grade": _jit_abs(seed, 3, -0.2, 0.1)}),
    ]


def _tpl_ft_mall_tt(total_km: float, seed: int) -> list[Section]:
    """FT_MALL_TT — longer 5-part chop with sign flips."""
    lens = _split_poisson(total_km, 5, seed + 402, min_frac=0.5)
    kinds = ["flat", "false_flat_down", "flat", "false_flat_up", "flat"]
    grades = [
        _jit_abs(seed, 10, -0.2, 0.2),
        _jit_abs(seed, 11, -0.8, -0.4),
        _jit_abs(seed, 12, -0.1, 0.3),
        _jit_abs(seed, 13, 0.4, 0.8),
        _jit_abs(seed, 14, -0.2, 0.2),
    ]
    return [Section(k, L, {"grade": g}) for k, L, g in zip(kinds, lens, grades)]


def _tpl_ft_london_classique(total_km: float, seed: int) -> list[Section]:
    """FT_LONDON_CLASSIQUE — flat with one barely-there mid-course bump."""
    lens = _split_poisson(total_km, 3, seed + 403, min_frac=0.5)
    return [
        Section("flat", lens[0], {"grade": _jit_abs(seed, 20, -0.1, 0.2)}),
        Section("short_hill", min(1.0, lens[1]),
                {"peak": _jit_abs(seed, 21, 1.2, 2.2)}),
        Section("flat", total_km - lens[0] - min(1.0, lens[1]),
                {"grade": _jit_abs(seed, 22, -0.1, 0.2)}),
    ]


_FT_TT_TEMPLATES = [
    _tpl_ft_tempus_fugit, _tpl_ft_volcano_flat, _tpl_ft_tick_tock,
    _tpl_ft_big_flat_8, _tpl_ft_mall_tt, _tpl_ft_london_classique,
]


def flat_tt(total_km: float, seed: int) -> ArchetypeOutput:
    """Pure flat TT — 6 real-world shape templates (Tempus Fugit family)."""
    t = _pick_template(seed, len(_FT_TT_TEMPLATES))
    sections = _FT_TT_TEMPLATES[t](total_km, seed)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "flat", "sprint_flat",
                     f"flat_tt_T{t}")


# --- flat_with_sprint templates (FT_SPRINT_FINISH / FT_CRIT_CITY_SPRINT /
#                                  FT_VOLCANO_SPRINT) ---

def _tpl_ft_sprint_finish(total_km: float, seed: int) -> list[Section]:
    """FT_SPRINT_FINISH — flat body + short drag to line."""
    tail = _jit_abs(seed, 1, 0.6, 1.1)
    body = total_km - tail
    return [
        Section("flat", body, {"grade": _jit_abs(seed, 2, -0.1, 0.3)}),
        Section("kicker_up", tail, {"peak": _jit_abs(seed, 3, 2.2, 3.2)}),
    ]


def _tpl_ft_crit_city_sprint(total_km: float, seed: int) -> list[Section]:
    """FT_CRIT_CITY_SPRINT — flat then false flat then kicker."""
    tail = _jit_abs(seed, 1, 0.5, 0.9)
    remain = total_km - tail
    lens = _split_poisson(remain, 2, seed + 501, min_frac=0.6)
    return [
        Section("flat", lens[0], {"grade": _jit_abs(seed, 2, -0.1, 0.2)}),
        Section("false_flat_up", lens[1], {"grade": _jit_abs(seed, 3, 0.6, 1.1)}),
        Section("kicker_up", tail, {"peak": _jit_abs(seed, 4, 2.8, 4.0)}),
    ]


def _tpl_ft_volcano_sprint(total_km: float, seed: int) -> list[Section]:
    """FT_VOLCANO_SPRINT — tiny roll body + short_hill finish."""
    tail = _jit_abs(seed, 1, 0.8, 1.3)
    body = total_km - tail
    return [
        Section("rolling", body,
                {"baseline": _jit_abs(seed, 2, 0.0, 0.3),
                 "amp": _jit_abs(seed, 3, 0.5, 1.0),
                 "period_km": _jit_abs(seed, 4, 2.5, 3.5)}),
        Section("short_hill", tail, {"peak": _jit_abs(seed, 5, 3.2, 4.4)}),
    ]


def _tpl_ft_berlin_sprint(total_km: float, seed: int) -> list[Section]:
    """Gently-descending body + sprint ramp (Berlin/Classique style)."""
    tail = _jit_abs(seed, 1, 0.6, 1.0)
    body = total_km - tail
    return [
        Section("false_flat_down", body, {"grade": _jit_abs(seed, 2, -0.4, -0.1)}),
        Section("kicker_up", tail, {"peak": _jit_abs(seed, 3, 2.8, 3.6)}),
    ]


_FT_SPRINT_TEMPLATES = [
    _tpl_ft_sprint_finish, _tpl_ft_crit_city_sprint,
    _tpl_ft_volcano_sprint, _tpl_ft_berlin_sprint,
]


def flat_with_sprint(total_km: float, seed: int) -> ArchetypeOutput:
    """Flat body + short final drag/sprint ramp. 4 templates."""
    t = _pick_template(seed, len(_FT_SPRINT_TEMPLATES))
    sections = _FT_SPRINT_TEMPLATES[t](total_km, seed)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "flat", "sprint_flat",
                     f"flat_with_sprint_T{t}")


# --- flat_with_hill_end templates (FT_HILL_FINISH / FT_BOLOGNA_RAMP_TO_LINE /
#                                    FT_RICHMOND_LIBBY_FINISH /
#                                    MX_INNSBRUCKRING / FT_MURDE_BRETAGNE_LITE) ---

def _tpl_ft_hill_finish(total_km: float, seed: int) -> list[Section]:
    climb_km = _jit_abs(seed, 1, 1.8, 3.0)
    body = total_km - climb_km
    climb_avg = _jit_abs(seed, 2, 4.0, 7.0)
    return [
        Section("flat", body, {"grade": _jit_abs(seed, 3, 0.0, 0.3)}),
        Section("gradual_climb", climb_km, {"avg": climb_avg, "roll_amp": 1.0}),
    ]


def _tpl_ft_bologna_ramp_to_line(total_km: float, seed: int) -> list[Section]:
    """Bologna — rolling approach + sustained climb to the line."""
    climb_km = _jit_abs(seed, 1, 2.0, 3.0)
    body = total_km - climb_km
    body_lens = _split_poisson(body, 2, seed + 601, min_frac=0.6)
    climb_avg = _jit_abs(seed, 5, 4.5, 6.5)
    return [
        Section("flat", body_lens[0], {"grade": _jit_abs(seed, 2, -0.1, 0.2)}),
        Section("false_flat_up", body_lens[1], {"grade": _jit_abs(seed, 3, 1.2, 2.0)}),
        Section("sustained_climb", climb_km, {"avg": climb_avg, "roll_amp": 1.2}),
    ]


def _tpl_ft_richmond_libby_finish(total_km: float, seed: int) -> list[Section]:
    """Richmond — three-kicker finish (Libby / 23rd St / Governor)."""
    tail_total = _jit_abs(seed, 1, 2.5, 3.5)
    body = total_km - tail_total
    # Libby / descent / 23rd / governor false flat
    libby = _jit_abs(seed, 2, 0.55, 0.75)
    desc = _jit_abs(seed, 3, 0.25, 0.45)
    wall = _jit_abs(seed, 4, 0.25, 0.40)
    gov = tail_total - libby - desc - wall
    return [
        Section("flat", body, {"grade": _jit_abs(seed, 5, 0.0, 0.3)}),
        Section("short_hill", libby, {"peak": _jit_abs(seed, 6, 6.5, 8.5)}),
        Section("descent", desc, {"grade": _jit_abs(seed, 7, -5.5, -3.5)}),
        Section("kicker_up", wall, {"peak": _jit_abs(seed, 8, 9.0, 11.0)}),
        Section("false_flat_up", max(0.2, gov), {"grade": _jit_abs(seed, 9, 3.0, 4.2)}),
    ]


def _tpl_ft_innsbruckring(total_km: float, seed: int) -> list[Section]:
    """MX_INNSBRUCKRING — flat loop with one Leg Snapper wall then flat."""
    wall = _jit_abs(seed, 1, 0.35, 0.55)
    after = _jit_abs(seed, 2, 1.5, 2.5)
    body = total_km - wall - after
    return [
        Section("flat", body, {"grade": _jit_abs(seed, 3, 0.0, 0.3)}),
        Section("kicker_up", wall, {"peak": _jit_abs(seed, 4, 7.0, 9.0)}),
        Section("descent", after * 0.35, {"grade": _jit_abs(seed, 5, -3.5, -2.0)}),
        Section("flat", after * 0.65, {"grade": _jit_abs(seed, 6, -0.1, 0.2)}),
    ]


_FT_HILL_END_TEMPLATES = [
    _tpl_ft_hill_finish, _tpl_ft_bologna_ramp_to_line,
    _tpl_ft_richmond_libby_finish, _tpl_ft_innsbruckring,
]


def flat_with_hill_end(total_km: float, seed: int) -> ArchetypeOutput:
    """Flat body + short climb finish. 4 templates, sim-library anchored."""
    t = _pick_template(seed, len(_FT_HILL_END_TEMPLATES))
    sections = _FT_HILL_END_TEMPLATES[t](total_km, seed)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "mixed", "summit",
                     f"flat_with_hill_end_T{t}")


# --- flat_descending_tt templates (FT_DESCENDING_TT / FT_GENTLY_DROPPING /
#                                    FT_DOWNHILL_WITH_RISE) ---

def _tpl_ft_descending_tt(total_km: float, seed: int) -> list[Section]:
    """FT_DESCENDING_TT — constant gentle descent."""
    return [Section("false_flat_down", total_km,
                    {"grade": _jit_abs(seed, 1, -1.4, -0.7)})]


def _tpl_ft_gently_dropping(total_km: float, seed: int) -> list[Section]:
    """FT_GENTLY_DROPPING — flat opening then descent."""
    lens = _split_poisson(total_km, 2, seed + 701, min_frac=0.5)
    return [
        Section("flat", lens[0], {"grade": _jit_abs(seed, 1, -0.1, 0.2)}),
        Section("false_flat_down", lens[1], {"grade": _jit_abs(seed, 2, -1.8, -0.8)}),
    ]


def _tpl_ft_rolling_descent(total_km: float, seed: int) -> list[Section]:
    """Descending with mid flat shelf."""
    lens = _split_poisson(total_km, 3, seed + 702, min_frac=0.4)
    return [
        Section("false_flat_down", lens[0], {"grade": _jit_abs(seed, 1, -1.4, -0.8)}),
        Section("flat", lens[1], {"grade": _jit_abs(seed, 2, -0.4, 0.0)}),
        Section("false_flat_down", lens[2], {"grade": _jit_abs(seed, 3, -1.3, -0.7)}),
    ]


_FT_DESCENDING_TEMPLATES = [
    _tpl_ft_descending_tt, _tpl_ft_gently_dropping, _tpl_ft_rolling_descent,
]


def flat_descending_tt(total_km: float, seed: int) -> ArchetypeOutput:
    """Slight net downhill TT. 3 templates."""
    t = _pick_template(seed, len(_FT_DESCENDING_TEMPLATES))
    sections = _FT_DESCENDING_TEMPLATES[t](total_km, seed)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "flat", "descent",
                     f"flat_descending_tt_T{t}")


# ── ROLLING FAMILY (4) ───────────────────────────────────────────────────────
#
# Aperiodic: every hill/flat block uses _jit_abs and _split_poisson. Never
# re-use equal-spacing patterns — those were the source of the lap_rolling
# and figure_8 max_autocorr≈0.97 failures.

# --- rolling_easy (Surrey Hills / Hilly Radio Tower lite / Gentle Undulations) ---

def _tpl_rl_surrey_hills(total_km: float, seed: int) -> list[Section]:
    """RL_SURREY_HILLS — gentle undulations with 3 aperiodic short hills."""
    sections: list[Section] = [
        Section("flat", _jit_abs(seed, 1, 0.6, 1.0),
                {"grade": _jit_abs(seed, 2, -0.1, 0.2)})
    ]
    remain = total_km - sections[0].length_km
    n_hills = 3
    hill_ws = [_jit_abs(seed, 100 + i, 0.6, 1.0) for i in range(n_hills)]
    gaps = _split_poisson(remain - sum(hill_ws), n_hills + 1, seed + 801, min_frac=0.4)
    for k in range(n_hills):
        sections.append(Section("flat", gaps[k], {"grade": _jit_abs(seed, 200 + k, 0.0, 0.3)}))
        sections.append(Section("short_hill", hill_ws[k],
                                {"peak": _jit_abs(seed, 300 + k, 3.5, 5.5)}))
    sections.append(Section("flat", gaps[-1], {"grade": _jit_abs(seed, 400, -0.1, 0.2)}))
    return sections


def _tpl_rl_hilly_radio_lite(total_km: float, seed: int) -> list[Section]:
    """RL_HILLY_RADIO_TOWER_LITE — rolling body + 2 modest hills asymmetric."""
    intro = _jit_abs(seed, 1, 1.0, 1.6)
    body_frac = _jit_abs(seed, 2, 0.35, 0.55)
    h1 = _jit_abs(seed, 3, 0.7, 1.0)
    h2 = _jit_abs(seed, 4, 0.6, 0.9)
    body_len = (total_km - intro - h1 - h2) * body_frac
    tail_len = total_km - intro - body_len - h1 - h2
    return [
        Section("flat", intro, {"grade": _jit_abs(seed, 5, -0.1, 0.2)}),
        Section("rolling", body_len,
                {"baseline": _jit_abs(seed, 6, 0.0, 0.3),
                 "amp": _jit_abs(seed, 7, 1.8, 2.4),
                 "period_km": _jit_abs(seed, 8, 1.7, 2.5)}),
        Section("short_hill", h1, {"peak": _jit_abs(seed, 9, 4.5, 5.5)}),
        Section("rolling", max(0.5, tail_len * 0.6),
                {"baseline": _jit_abs(seed, 10, -0.1, 0.2),
                 "amp": _jit_abs(seed, 11, 1.5, 2.2),
                 "period_km": _jit_abs(seed, 12, 1.5, 2.2)}),
        Section("short_hill", h2, {"peak": _jit_abs(seed, 13, 4.5, 5.8)}),
        Section("flat", max(0.3, tail_len * 0.4),
                {"grade": _jit_abs(seed, 14, -0.2, 0.2)}),
    ]


def _tpl_rl_gentle_undulations(total_km: float, seed: int) -> list[Section]:
    """RL_GENTLE_UNDULATIONS — pure rolling, wide spacing."""
    return [Section("rolling", total_km,
                    {"baseline": _jit_abs(seed, 1, -0.1, 0.3),
                     "amp": _jit_abs(seed, 2, 2.2, 3.0),
                     "period_km": _jit_abs(seed, 3, 2.0, 3.2)})]


def _tpl_rl_midroute_cluster(total_km: float, seed: int) -> list[Section]:
    """RL_MIDROUTE_CLUSTER — cluster of modest hills in mid-route."""
    intro = _jit_abs(seed, 1, 1.5, 2.5)
    cluster_km = _jit_abs(seed, 2, total_km * 0.30, total_km * 0.45)
    tail = total_km - intro - cluster_km
    sections: list[Section] = [
        Section("flat", intro, {"grade": _jit_abs(seed, 3, -0.1, 0.3)})
    ]
    # 3 hills inside cluster, aperiodic
    n = 3
    hill_ws = [_jit_abs(seed, 100 + i, 0.55, 0.85) for i in range(n)]
    gaps = _split_poisson(max(0.3, cluster_km - sum(hill_ws)),
                          n + 1, seed + 811, min_frac=0.3)
    for k in range(n):
        sections.append(Section("flat", gaps[k],
                                {"grade": _jit_abs(seed, 200 + k, 0.0, 0.3)}))
        sections.append(Section("short_hill", hill_ws[k],
                                {"peak": _jit_abs(seed, 300 + k, 4.0, 5.8)}))
    sections.append(Section("flat", gaps[-1],
                            {"grade": _jit_abs(seed, 301, -0.1, 0.2)}))
    sections.append(Section("flat", max(0.5, tail),
                            {"grade": _jit_abs(seed, 302, -0.2, 0.1)}))
    return sections


def _tpl_rl_ascending_difficulty(total_km: float, seed: int) -> list[Section]:
    """Grade steadily increases — 3 hills each harder than last."""
    intro = _jit_abs(seed, 1, 0.8, 1.4)
    remain = total_km - intro
    n = 3
    hill_ws = [_jit_abs(seed, 100 + i, 0.7, 0.9) for i in range(n)]
    gaps = _split_poisson(remain - sum(hill_ws), n + 1, seed + 821, min_frac=0.4)
    sections = [Section("flat", intro, {"grade": _jit_abs(seed, 2, 0.0, 0.3)})]
    for k in range(n):
        sections.append(Section("flat", gaps[k],
                                {"grade": _jit_abs(seed, 200 + k, 0.0, 0.2)}))
        sections.append(Section("short_hill", hill_ws[k],
                                {"peak": _jit_abs(seed, 300 + k, 3.5 + k * 1.0, 5.0 + k * 1.0)}))
    sections.append(Section("flat", gaps[-1], {"grade": _jit_abs(seed, 400, -0.1, 0.2)}))
    return sections


_ROLLING_EASY_TEMPLATES = [
    _tpl_rl_surrey_hills, _tpl_rl_hilly_radio_lite,
    _tpl_rl_gentle_undulations, _tpl_rl_midroute_cluster,
    _tpl_rl_ascending_difficulty,
]


def rolling_easy(total_km: float, seed: int) -> ArchetypeOutput:
    """Gentle rollers — 5 reference/real-world templates."""
    t = _pick_template(seed, len(_ROLLING_EASY_TEMPLATES))
    sections = _ROLLING_EASY_TEMPLATES[t](total_km, seed)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "rolling", "none",
                     f"rolling_easy_T{t}")


# --- rolling_punchy (Hilly / Front / Back / Scattered / Alternating /
#                     Innsbruck KOM repeats) ---

def _tpl_rl_hilly_route(total_km: float, seed: int) -> list[Section]:
    """RL_HILLY_ROUTE — single climb in first 15%, rolling rest."""
    intro = _jit_abs(seed, 1, 0.4, 0.7)
    climb = _jit_abs(seed, 2, 0.8, 1.1)
    remain = total_km - intro - climb
    lens = _split_poisson(remain, 3, seed + 901, min_frac=0.4)
    return [
        Section("flat", intro, {"grade": _jit_abs(seed, 3, -0.1, 0.3)}),
        Section("short_hill", climb, {"peak": _jit_abs(seed, 4, 5.5, 7.0)}),
        Section("rolling", lens[0],
                {"baseline": _jit_abs(seed, 5, 0.0, 0.4),
                 "amp": _jit_abs(seed, 6, 1.8, 2.5),
                 "period_km": _jit_abs(seed, 7, 1.6, 2.4)}),
        Section("flat", lens[1], {"grade": _jit_abs(seed, 8, -0.2, 0.1)}),
        Section("rolling", lens[2],
                {"baseline": _jit_abs(seed, 9, 0.0, 0.3),
                 "amp": _jit_abs(seed, 10, 1.5, 2.2),
                 "period_km": _jit_abs(seed, 11, 1.5, 2.2)}),
    ]


def _tpl_rl_front_loaded(total_km: float, seed: int) -> list[Section]:
    """RL_FRONT_LOADED — hills clustered early, flat tail."""
    intro = _jit_abs(seed, 1, 0.5, 0.9)
    cluster = _jit_abs(seed, 2, total_km * 0.30, total_km * 0.45)
    tail = total_km - intro - cluster
    n = 3
    hill_ws = [_jit_abs(seed, 100 + i, 0.45, 0.70) for i in range(n)]
    gaps = _split_poisson(max(0.5, cluster - sum(hill_ws)),
                          n + 1, seed + 911, min_frac=0.3)
    peaks = [_jit_abs(seed, 200 + i, 7.0, 10.0) for i in range(n)]
    sections = [Section("flat", intro, {"grade": _jit_abs(seed, 3, 0.0, 0.3)})]
    for k in range(n):
        sections.append(Section("flat", gaps[k], {"grade": _jit_abs(seed, 300 + k, 0.0, 0.3)}))
        kind = "kicker_up" if peaks[k] > 8.5 else "short_hill"
        sections.append(Section(kind, hill_ws[k], {"peak": peaks[k]}))
    sections.append(Section("flat", gaps[-1] + tail * 0.5, {"grade": _jit_abs(seed, 400, -0.1, 0.2)}))
    sections.append(Section("rolling", max(0.3, tail * 0.5),
                            {"baseline": _jit_abs(seed, 401, 0.0, 0.3),
                             "amp": _jit_abs(seed, 402, 0.8, 1.5),
                             "period_km": _jit_abs(seed, 403, 2.0, 3.0)}))
    return sections


def _tpl_rl_back_loaded(total_km: float, seed: int) -> list[Section]:
    """RL_BACK_LOADED — flat intro, hills clustered toward end."""
    intro_frac = _jit_abs(seed, 1, 0.40, 0.55)
    intro_km = total_km * intro_frac
    cluster = total_km - intro_km
    n = 3
    hill_ws = [_jit_abs(seed, 100 + i, 0.45, 0.70) for i in range(n)]
    gaps = _split_poisson(max(0.5, cluster - sum(hill_ws)),
                          n + 1, seed + 912, min_frac=0.3)
    peaks = [_jit_abs(seed, 200 + i, 6.5, 9.5) for i in range(n)]
    sections = [
        Section("flat", intro_km * 0.6, {"grade": _jit_abs(seed, 2, -0.1, 0.2)}),
        Section("rolling", intro_km * 0.4,
                {"baseline": _jit_abs(seed, 3, 0.0, 0.3),
                 "amp": _jit_abs(seed, 4, 1.0, 1.8),
                 "period_km": _jit_abs(seed, 5, 2.2, 3.0)}),
    ]
    for k in range(n):
        sections.append(Section("flat", gaps[k], {"grade": _jit_abs(seed, 300 + k, 0.0, 0.3)}))
        kind = "kicker_up" if peaks[k] > 8.5 else "short_hill"
        sections.append(Section(kind, hill_ws[k], {"peak": peaks[k]}))
    sections.append(Section("flat", gaps[-1], {"grade": _jit_abs(seed, 400, -0.2, 0.2)}))
    return sections


def _tpl_rl_scattered_kickers(total_km: float, seed: int) -> list[Section]:
    """RL_SCATTERED_KICKERS — 4 kickers at Poisson-placed positions."""
    intro = _jit_abs(seed, 1, 0.7, 1.1)
    remain = total_km - intro
    n = 4
    hill_ws = [_jit_abs(seed, 100 + i, 0.35, 0.55) for i in range(n)]
    gaps = _split_poisson(remain - sum(hill_ws), n + 1, seed + 913, min_frac=0.35)
    peaks = [_jit_abs(seed, 200 + i, 7.5, 10.5) for i in range(n)]
    sections = [Section("flat", intro, {"grade": _jit_abs(seed, 2, 0.0, 0.3)})]
    for k in range(n):
        sections.append(Section("flat", gaps[k], {"grade": _jit_abs(seed, 300 + k, 0.0, 0.3)}))
        sections.append(Section("kicker_up", hill_ws[k], {"peak": peaks[k]}))
    sections.append(Section("flat", gaps[-1], {"grade": _jit_abs(seed, 400, -0.1, 0.2)}))
    return sections


def _tpl_rl_alternating_flats(total_km: float, seed: int) -> list[Section]:
    """RL_ALTERNATING_FLATS — 3 hills between long irregular flats."""
    intro = _jit_abs(seed, 1, 1.2, 2.0)
    remain = total_km - intro
    n = 3
    hill_ws = [_jit_abs(seed, 100 + i, 0.55, 0.80) for i in range(n)]
    gaps = _split_poisson(remain - sum(hill_ws), n + 1, seed + 914, min_frac=0.5)
    sections = [Section("flat", intro, {"grade": _jit_abs(seed, 2, -0.1, 0.2)})]
    for k in range(n):
        sections.append(Section("flat", gaps[k], {"grade": _jit_abs(seed, 300 + k, 0.0, 0.3)}))
        peak = _jit_abs(seed, 200 + k, 6.0, 9.0)
        kind = "kicker_up" if peak > 8.0 else "short_hill"
        sections.append(Section(kind, hill_ws[k], {"peak": peak}))
    sections.append(Section("flat", gaps[-1], {"grade": _jit_abs(seed, 400, -0.2, 0.2)}))
    return sections


def _tpl_rl_innsbruck_kom_repeats(total_km: float, seed: int) -> list[Section]:
    """RL_INNSBRUCK_KOM_REPEATS — Leg Snapper finish after rolling approach."""
    wall_len = _jit_abs(seed, 1, 0.35, 0.55)
    tail = _jit_abs(seed, 2, 1.0, 2.0)
    body = total_km - wall_len - tail
    body_lens = _split_poisson(body, 3, seed + 915, min_frac=0.5)
    return [
        Section("flat", body_lens[0], {"grade": _jit_abs(seed, 3, -0.1, 0.2)}),
        Section("rolling", body_lens[1],
                {"baseline": _jit_abs(seed, 4, 0.0, 0.3),
                 "amp": _jit_abs(seed, 5, 1.5, 2.3),
                 "period_km": _jit_abs(seed, 6, 1.5, 2.3)}),
        Section("flat", body_lens[2], {"grade": _jit_abs(seed, 7, -0.1, 0.2)}),
        Section("kicker_up", wall_len, {"peak": _jit_abs(seed, 8, 7.5, 9.5)}),
        Section("descent", tail * 0.4, {"grade": _jit_abs(seed, 9, -4.0, -2.5)}),
        Section("flat", max(0.2, tail * 0.6), {"grade": _jit_abs(seed, 10, -0.2, 0.2)}),
    ]


_ROLLING_PUNCHY_TEMPLATES = [
    _tpl_rl_hilly_route, _tpl_rl_front_loaded, _tpl_rl_back_loaded,
    _tpl_rl_scattered_kickers, _tpl_rl_alternating_flats,
    _tpl_rl_innsbruck_kom_repeats,
]


def rolling_punchy(total_km: float, seed: int) -> ArchetypeOutput:
    """Rolling + 3-5 punchy kickers. 6 reference/real-world templates."""
    t = _pick_template(seed, len(_ROLLING_PUNCHY_TEMPLATES))
    sections = _ROLLING_PUNCHY_TEMPLATES[t](total_km, seed)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "rolling", "none",
                     f"rolling_punchy_T{t}")


# --- figure_8 (two-peak / mirror / two-hop / lollipop) ---

def _tpl_f8_two_peaks(total_km: float, seed: int) -> list[Section]:
    """RL_FIGURE_8_TWO_PEAKS — two non-identical peaks separated by a descent."""
    half1 = total_km * _jit_abs(seed, 1, 0.42, 0.52)
    half2 = total_km - half1
    climb1 = half1 * _jit_abs(seed, 2, 0.28, 0.40)
    descent1 = half1 * _jit_abs(seed, 3, 0.20, 0.32)
    flat1 = half1 - climb1 - descent1
    climb2 = half2 * _jit_abs(seed, 4, 0.28, 0.40)
    descent2 = half2 * _jit_abs(seed, 5, 0.20, 0.32)
    flat2 = half2 - climb2 - descent2
    return [
        Section("false_flat_up", flat1 * 0.5, {"grade": _jit_abs(seed, 6, 1.5, 2.5)}),
        Section("gradual_climb", climb1, {"avg": _jit_abs(seed, 7, 4.0, 5.5),
                                          "roll_amp": 1.0}),
        Section("descent", descent1, {"grade": _jit_abs(seed, 8, -5.0, -3.5)}),
        Section("flat", max(0.2, flat1 * 0.5), {"grade": _jit_abs(seed, 9, -0.3, 0.2)}),
        Section("false_flat_up", flat2 * 0.3, {"grade": _jit_abs(seed, 10, 1.8, 2.8)}),
        Section("gradual_climb", climb2, {"avg": _jit_abs(seed, 11, 4.5, 6.0),
                                          "roll_amp": 1.0}),
        Section("descent", descent2, {"grade": _jit_abs(seed, 12, -5.0, -3.5)}),
        Section("flat", max(0.2, flat2 * 0.7), {"grade": _jit_abs(seed, 13, -0.3, 0.2)}),
    ]


def _tpl_f8_mirror(total_km: float, seed: int) -> list[Section]:
    """RL_FIGURE_8_MIRROR — second half deliberately mirrors but with jitter."""
    climb_km = _jit_abs(seed, 1, total_km * 0.22, total_km * 0.30)
    descent_km = _jit_abs(seed, 2, total_km * 0.18, total_km * 0.25)
    # hill then descent then flat + repeat
    flat_pad = (total_km - 2 * (climb_km + descent_km)) / 2
    return [
        Section("short_hill", climb_km, {"peak": _jit_abs(seed, 3, 5.0, 6.5)}),
        Section("descent", descent_km, {"grade": _jit_abs(seed, 4, -5.0, -3.5)}),
        Section("flat", max(0.2, flat_pad), {"grade": _jit_abs(seed, 5, -0.2, 0.2)}),
        Section("short_hill", climb_km * _jit_abs(seed, 6, 0.85, 1.15),
                {"peak": _jit_abs(seed, 7, 5.0, 6.5)}),
        Section("descent", descent_km * _jit_abs(seed, 8, 0.85, 1.15),
                {"grade": _jit_abs(seed, 9, -5.0, -3.5)}),
        Section("flat", max(0.2, flat_pad), {"grade": _jit_abs(seed, 10, -0.2, 0.2)}),
    ]


def _tpl_f8_rolling_two_halves(total_km: float, seed: int) -> list[Section]:
    """Two rolling loops with different characteristics (not literal repeats)."""
    half1 = total_km * _jit_abs(seed, 1, 0.45, 0.55)
    half2 = total_km - half1
    return [
        Section("rolling", half1,
                {"baseline": _jit_abs(seed, 2, 0.2, 0.6),
                 "amp": _jit_abs(seed, 3, 3.0, 4.0),
                 "period_km": _jit_abs(seed, 4, 2.2, 3.2)}),
        Section("rolling", half2,
                {"baseline": _jit_abs(seed, 5, -0.4, 0.0),
                 "amp": _jit_abs(seed, 6, 3.2, 4.2),
                 "period_km": _jit_abs(seed, 7, 1.8, 2.8)}),
    ]


_FIGURE_8_TEMPLATES = [
    _tpl_f8_two_peaks, _tpl_f8_mirror, _tpl_f8_rolling_two_halves,
]


def figure_8(total_km: float, seed: int) -> ArchetypeOutput:
    """Figure-8 profile. 3 templates — NEVER a literal duplicate-half."""
    t = _pick_template(seed, len(_FIGURE_8_TEMPLATES))
    sections = _FIGURE_8_TEMPLATES[t](total_km, seed)
    sections = _scale_to(total_km, sections)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "rolling", "none",
                     f"figure_8_T{t}")


# --- rolling_with_climb_finish (HILLY ROUTE / MOUNTAIN KOM /
#                                 BOX HILL LITE) ---

def _tpl_rl_hilly_climb_finish(total_km: float, seed: int) -> list[Section]:
    climb_km = _jit_abs(seed, 1, min(5.5, total_km * 0.25), min(6.5, total_km * 0.35))
    body = total_km - climb_km
    body_lens = _split_poisson(body, 2, seed + 1001, min_frac=0.4)
    return [
        Section("rolling", body_lens[0],
                {"baseline": _jit_abs(seed, 2, 0.1, 0.4),
                 "amp": _jit_abs(seed, 3, 2.2, 3.0),
                 "period_km": _jit_abs(seed, 4, 1.8, 2.4)}),
        Section("flat", body_lens[1], {"grade": _jit_abs(seed, 5, -0.1, 0.3)}),
        Section("gradual_climb", climb_km,
                {"avg": _jit_abs(seed, 6, 5.0, 7.0), "roll_amp": 1.2}),
    ]


def _tpl_rl_mountain_kom_finish(total_km: float, seed: int) -> list[Section]:
    """Flat + rolling + false flat + sustained climb."""
    climb_km = _jit_abs(seed, 1, min(5.0, total_km * 0.28), min(6.0, total_km * 0.38))
    body = total_km - climb_km
    body_lens = _split_poisson(body, 3, seed + 1002, min_frac=0.3)
    climb_avg = _jit_abs(seed, 2, 5.5, 7.5)
    return [
        Section("flat", body_lens[0], {"grade": _jit_abs(seed, 3, -0.1, 0.2)}),
        Section("rolling", body_lens[1],
                {"baseline": _jit_abs(seed, 4, 0.1, 0.4),
                 "amp": _jit_abs(seed, 5, 1.8, 2.6),
                 "period_km": _jit_abs(seed, 6, 2.0, 2.8)}),
        Section("false_flat_up", body_lens[2], {"grade": _jit_abs(seed, 7, 2.0, 3.0)}),
        Section("sustained_climb", climb_km,
                {"avg": climb_avg, "roll_amp": 1.5}),
    ]


def _tpl_rl_mini_kicker_then_climb(total_km: float, seed: int) -> list[Section]:
    """Rolling punchy body then sustained climb to finish."""
    climb_km = _jit_abs(seed, 1, min(5.0, total_km * 0.25), min(6.0, total_km * 0.33))
    body = total_km - climb_km
    body_lens = _split_poisson(body, 4, seed + 1003, min_frac=0.3)
    h1 = _jit_abs(seed, 2, 0.55, 0.85)
    body_lens[0] = max(0.3, body_lens[0] - h1)
    climb_avg = _jit_abs(seed, 3, 5.0, 6.8)
    return [
        Section("flat", body_lens[0], {"grade": _jit_abs(seed, 4, -0.1, 0.2)}),
        Section("short_hill", h1, {"peak": _jit_abs(seed, 5, 5.0, 6.5)}),
        Section("rolling", body_lens[1],
                {"baseline": _jit_abs(seed, 6, 0.1, 0.4),
                 "amp": _jit_abs(seed, 7, 1.8, 2.6),
                 "period_km": _jit_abs(seed, 8, 1.8, 2.4)}),
        Section("flat", body_lens[2], {"grade": _jit_abs(seed, 9, -0.1, 0.2)}),
        Section("false_flat_up", body_lens[3], {"grade": _jit_abs(seed, 10, 1.5, 2.5)}),
        Section("gradual_climb", climb_km, {"avg": climb_avg, "roll_amp": 1.3}),
    ]


_ROLLING_CLIMB_FINISH_TEMPLATES = [
    _tpl_rl_hilly_climb_finish, _tpl_rl_mountain_kom_finish,
    _tpl_rl_mini_kicker_then_climb,
]


def rolling_with_climb_finish(total_km: float, seed: int) -> ArchetypeOutput:
    """Rolling body + climb finish. 3 templates."""
    t = _pick_template(seed, len(_ROLLING_CLIMB_FINISH_TEMPLATES))
    sections = _ROLLING_CLIMB_FINISH_TEMPLATES[t](total_km, seed)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "rolling", "summit",
                     f"rolling_with_climb_finish_T{t}")


# ── CLIMB FAMILY (11) ────────────────────────────────────────────────────────
#
# Each template maps to a reference/real-world climb signature. Lead-ins are
# per-seed (30% by default, higher for long climbs). Section lengths use
# jitter helpers — no equal-slice composition.

# ── wall: WL_RADIO_TOWER / WL_SWAINS_LANE / WL_MONTGO_SPIKE /
#          WL_MUR_DL_GIAT_STEPPED / WL_GOVERNOR_STREET_MONOTONIC ──

def _tpl_wl_radio_tower(total_km: float, seed: int) -> list[Section]:
    """WL_RADIO_TOWER — 4-6% opening then sustained 12-15%, final ramp to 17%+."""
    warm = _jit_abs(seed, 1, 0.08, 0.15)
    ramp_final = _jit_abs(seed, 2, 0.15, 0.25)
    mid = total_km - warm - ramp_final
    return [
        Section("false_flat_up", warm, {"grade": _jit_abs(seed, 3, 4.0, 6.0)}),
        Section("steep_wall", mid, {"avg": _jit_abs(seed, 4, 12.5, 14.5)}),
        Section("steep_wall", ramp_final, {"avg": _jit_abs(seed, 5, 16.5, 18.5)}),
    ]


def _tpl_wl_swains_lane(total_km: float, seed: int) -> list[Section]:
    """WL_SWAINS_LANE — 6-8% opening, 11-13% middle, brief taper to finish."""
    warm = _jit_abs(seed, 1, 0.10, 0.18)
    taper = _jit_abs(seed, 2, 0.12, 0.20)
    mid = total_km - warm - taper
    return [
        Section("false_flat_up", warm, {"grade": _jit_abs(seed, 3, 6.0, 8.0)}),
        Section("steep_wall", mid, {"avg": _jit_abs(seed, 4, 11.0, 13.5)}),
        Section("sustained_climb", taper,
                {"avg": _jit_abs(seed, 5, 7.5, 9.0), "roll_amp": 1.0}),
    ]


def _tpl_wl_montgo_spike(total_km: float, seed: int) -> list[Section]:
    """WL_MONTGO_SPIKE — single-spike wall: 3-7% approach, 19-21% spike, tail."""
    approach_frac = _jit_abs(seed, 1, 0.35, 0.50)
    spike_frac = _jit_abs(seed, 2, 0.15, 0.25)
    approach = total_km * approach_frac
    spike = total_km * spike_frac
    tail = total_km - approach - spike
    return [
        Section("gradual_climb", approach,
                {"avg": _jit_abs(seed, 3, 4.0, 6.0), "roll_amp": 0.8}),
        Section("steep_wall", spike, {"avg": _jit_abs(seed, 4, 17.5, 19.5)}),
        Section("descent", tail, {"grade": _jit_abs(seed, 5, -3.0, -1.0)}),
    ]


def _tpl_wl_mur_dl_giat(total_km: float, seed: int) -> list[Section]:
    """WL_MUR_DL_GIAT_STEPPED — aperiodic stepped wall with one peak spike."""
    n = 3 + int(seeded_random(seed, 1) * 2)  # 3 or 4 steps
    sections: list[Section] = []
    # Intro ramp
    intro = _jit_abs(seed, 2, 0.08, 0.15)
    sections.append(Section("false_flat_up", intro,
                            {"grade": _jit_abs(seed, 3, 3.0, 5.0)}))
    remain = total_km - intro
    # Each step: steep wall + short relief. Aperiodic lengths.
    step_wall_lens = _split_poisson(remain * 0.7, n, seed + 1101, min_frac=0.6)
    step_relief_lens = _split_poisson(remain * 0.3, n, seed + 1102, min_frac=0.4)
    # One step gets the peak spike
    spike_idx = int(seeded_random(seed, 4) * n)
    for k in range(n):
        peak = _jit_abs(seed, 100 + k, 7.0, 10.0)
        if k == spike_idx:
            peak = _jit_abs(seed, 50, 14.0, 17.0)
        sections.append(Section("steep_wall", step_wall_lens[k], {"avg": peak}))
        if k < n - 1:
            sections.append(Section("plateau", step_relief_lens[k],
                                    {"grade": _jit_abs(seed, 200 + k, 1.0, 3.0)}))
    return sections


def _tpl_wl_governor_street(total_km: float, seed: int) -> list[Section]:
    """WL_GOVERNOR_STREET_MONOTONIC — 50 m warmup, then 9-12% with texture."""
    warm = _jit_abs(seed, 1, 0.05, 0.12)
    return [
        Section("flat", warm, {"grade": _jit_abs(seed, 2, 0.5, 1.2)}),
        Section("steep_wall", total_km - warm,
                {"avg": _jit_abs(seed, 3, 9.0, 11.5)}),
    ]


_WALL_TEMPLATES = [
    _tpl_wl_radio_tower, _tpl_wl_swains_lane, _tpl_wl_montgo_spike,
    _tpl_wl_mur_dl_giat, _tpl_wl_governor_street,
]


def wall(total_km: float, seed: int) -> ArchetypeOutput:
    """Short punchy wall — 5 reference/real-world signatures."""
    t = _pick_template(seed, len(_WALL_TEMPLATES))
    sections = _WALL_TEMPLATES[t](total_km, seed)
    # Re-scale so total equals requested total_km (avoids sum drift
    # from primitive floor rounding in very short sections).
    sections = _scale_to(total_km, sections)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "climb", "wall",
                     f"wall_T{t}")


# ── cat4_short: CL_CAT4_SHORT_TEMPO / CL_CAT4_RAMP / CL_CAT4_BOX_HILL ──

def _tpl_cat4_short_tempo(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Gentle steady climb, one brief spike."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 4.0, 6.0)
    lens = _split_poisson(core, 3, seed + 1201, min_frac=0.5)
    return lead + [
        Section("gradual_climb", lens[0], {"avg": avg, "roll_amp": 1.1}),
        Section("kicker_up", lens[1], {"peak": _jit_abs(seed, 2, avg + 3.0, avg + 5.0)}),
        Section("gradual_climb", lens[2], {"avg": avg + 0.5, "roll_amp": 1.0}),
    ]


def _tpl_cat4_ramp(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Climb ramps progressively — 2 steps hard, no plateau."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 4.0, 6.0)
    lens = _split_poisson(core, 2, seed + 1202, min_frac=0.6)
    return lead + [
        Section("gradual_climb", lens[0],
                {"avg": avg - _jit_abs(seed, 2, 0.5, 1.2), "roll_amp": 1.1}),
        Section("sustained_climb", lens[1],
                {"avg": avg + _jit_abs(seed, 3, 0.5, 1.5), "roll_amp": 1.2}),
    ]


def _tpl_cat4_box_hill(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Box Hill rhythmic — low SD but aperiodic via rolling primitive."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 4.5, 5.8)
    return lead + [
        Section("rolling", core,
                {"baseline": avg,
                 "amp": _jit_abs(seed, 2, 1.0, 1.5),
                 "period_km": _jit_abs(seed, 3, 0.35, 0.55)}),
    ]


def _tpl_cat4_alpine_lite(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Alpine lite — short final kick."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 4.0, 6.0)
    lens = _split_poisson(core, 3, seed + 1203, min_frac=0.35)
    return lead + [
        Section("gradual_climb", lens[0], {"avg": avg, "roll_amp": 1.1}),
        Section("plateau", lens[1], {"grade": _jit_abs(seed, 2, 2.0, 3.5)}),
        Section("sustained_climb", lens[2],
                {"avg": avg + _jit_abs(seed, 3, 1.0, 2.0), "roll_amp": 1.1}),
    ]


_CAT4_TEMPLATES = [
    _tpl_cat4_short_tempo, _tpl_cat4_ramp, _tpl_cat4_box_hill, _tpl_cat4_alpine_lite,
]


def cat4_short(total_km: float, seed: int) -> ArchetypeOutput:
    """Short Cat-4 climb. 4 templates — 30% lead-in per seed."""
    t = _pick_template(seed, len(_CAT4_TEMPLATES))
    lead = _lead_in(seed, probability=0.30)
    sections = _CAT4_TEMPLATES[t](total_km, seed, lead)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "climb", "summit",
                     f"cat4_short_T{t}")


# ── cat3_tempo: CL_CAT3_LANZAROTE / CL_CAT3_TENERIFE / CL_CAT3_SA_CALOBRA ──

def _tpl_cat3_lanzarote(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """La Montaneta — punchy, then plateau, then tempo finish."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 5.0, 7.0)
    # proportions: intro 20%, kicker 15%, plateau 12%, sustained 45%, plateau 8%
    # jitter those proportions ±15%
    p = [_jit(seed, 100 + i, b, 0.18) for i, b in
         enumerate([0.20, 0.15, 0.12, 0.45, 0.08])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("gradual_climb", core * p[0],
                {"avg": avg - 1.0, "roll_amp": 0.9}),
        Section("kicker_up", core * p[1], {"peak": avg + 4.0}),
        Section("plateau", core * p[2], {"grade": _jit_abs(seed, 2, 2.0, 3.0)}),
        Section("sustained_climb", core * p[3],
                {"avg": avg + 0.5, "roll_amp": 1.2}),
        Section("plateau", core * p[4], {"grade": _jit_abs(seed, 3, 2.0, 3.0)}),
    ]


def _tpl_cat3_tenerife_lite(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Progressive tempo, one spike, taper finish."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 5.5, 7.0)
    lens = _split_poisson(core, 4, seed + 1301, min_frac=0.4)
    return lead + [
        Section("sustained_climb", lens[0], {"avg": avg, "roll_amp": 1.4}),
        Section("kicker_up", lens[1] * 0.4, {"peak": avg + 5.0}),
        Section("plateau", lens[1] * 0.6, {"grade": _jit_abs(seed, 2, 2.5, 3.5)}),
        Section("sustained_climb", lens[2], {"avg": avg + 0.5, "roll_amp": 1.4}),
        Section("gradual_climb", lens[3], {"avg": avg - 0.5, "roll_amp": 1.2}),
    ]


def _tpl_cat3_sa_calobra(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Sa Calobra partial — steady tempo with mild oscillation."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 5.5, 7.0)
    return lead + [Section("gradual_climb", core,
                           {"avg": avg, "roll_amp": _jit_abs(seed, 2, 1.2, 1.6)})]


def _tpl_cat3_jagged(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Collada-like jagged: spike-relief every ~1 km (aperiodic)."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 5.5, 7.0)
    n = max(3, int(core / _jit_abs(seed, 2, 1.6, 2.2)))
    wall_lens = _split_poisson(core * 0.60, n, seed + 1302, min_frac=0.4)
    relief_lens = _split_poisson(core * 0.40, n, seed + 1303, min_frac=0.3)
    sections = list(lead)
    for k in range(n):
        sections.append(Section("sustained_climb", wall_lens[k],
                                {"avg": avg + _jit_abs(seed, 100 + k, 0.3, 1.5),
                                 "roll_amp": 1.5}))
        sections.append(Section("plateau" if seeded_random(seed, 200 + k) < 0.5 else "gradual_climb",
                                relief_lens[k],
                                {"grade": _jit_abs(seed, 300 + k, avg - 2.5, avg - 1.0),
                                 "avg": avg - 1.0, "roll_amp": 0.8}))
    return sections


_CAT3_TEMPLATES = [
    _tpl_cat3_lanzarote, _tpl_cat3_tenerife_lite,
    _tpl_cat3_sa_calobra, _tpl_cat3_jagged,
]


def cat3_tempo(total_km: float, seed: int) -> ArchetypeOutput:
    """Cat-3 tempo climb. 4 templates."""
    t = _pick_template(seed, len(_CAT3_TEMPLATES))
    lead = _lead_in(seed, probability=0.30)
    sections = _CAT3_TEMPLATES[t](total_km, seed, lead)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "climb", "summit",
                     f"cat3_tempo_T{t}")


# ── cat2_ramps: CL_CAT2_COLLADA_BEIXALIS / CL_CAT2_TENERIFE /
#                 CL_CAT2_JAGGED / CL_CAT2_STEPPED ──

def _tpl_cat2_collada(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Collada Beixalis — spike-relief every ~1 km, aperiodic spacing."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 6.0, 8.0)
    n = max(4, int(core / _jit_abs(seed, 2, 1.2, 1.8)))
    climb_lens = _split_poisson(core * 0.65, n, seed + 1401, min_frac=0.4)
    kick_lens = _split_poisson(core * 0.35, n, seed + 1402, min_frac=0.3)
    sections = list(lead)
    for k in range(n):
        sections.append(Section("sustained_climb", climb_lens[k],
                                {"avg": avg + _jit_abs(seed, 100 + k, -0.5, 1.5),
                                 "roll_amp": 1.4}))
        # Relief vs kicker alternating but with jittered probability
        if seeded_random(seed, 200 + k) < 0.55:
            sections.append(Section("kicker_up", kick_lens[k],
                                    {"peak": avg + _jit_abs(seed, 300 + k, 4.0, 6.0)}))
        else:
            sections.append(Section("plateau", kick_lens[k],
                                    {"grade": _jit_abs(seed, 400 + k, avg - 3.0, avg - 1.5)}))
    return sections


def _tpl_cat2_tenerife(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Tenerife style — gradual approach then hard core, fade at top."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 6.0, 7.5)
    p = [_jit(seed, 100 + i, b, 0.15) for i, b in
         enumerate([0.30, 0.10, 0.40, 0.10, 0.10])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("gradual_climb", core * p[0],
                {"avg": avg - 1.0, "roll_amp": 1.2}),
        Section("plateau", core * p[1], {"grade": _jit_abs(seed, 2, 3.0, 4.0)}),
        Section("sustained_climb", core * p[2],
                {"avg": avg + 1.5, "roll_amp": 1.5}),
        Section("kicker_up", core * p[3], {"peak": avg + 5.0}),
        Section("sustained_climb", core * p[4], {"avg": avg, "roll_amp": 1.0}),
    ]


def _tpl_cat2_jagged(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Stepped, aperiodic sustained blocks of alternating intensity."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 6.0, 8.0)
    n = 4
    lens = _split_poisson(core, n, seed + 1403, min_frac=0.5)
    intensities = [_jit_abs(seed, 100 + k, -1.5, 2.0) for k in range(n)]
    return lead + [
        Section("sustained_climb", lens[k],
                {"avg": avg + intensities[k], "roll_amp": 1.4})
        for k in range(n)
    ]


def _tpl_cat2_stepped(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Stepped with two plateau interruptions."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 6.0, 8.0)
    p = [_jit(seed, 100 + i, b, 0.15) for i, b in
         enumerate([0.25, 0.08, 0.30, 0.10, 0.27])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("sustained_climb", core * p[0],
                {"avg": avg, "roll_amp": 1.5}),
        Section("plateau", core * p[1],
                {"grade": _jit_abs(seed, 2, 3.0, 4.0)}),
        Section("sustained_climb", core * p[2],
                {"avg": avg + 1.5, "roll_amp": 1.5}),
        Section("plateau", core * p[3],
                {"grade": _jit_abs(seed, 3, 3.5, 4.5)}),
        Section("sustained_climb", core * p[4],
                {"avg": avg + 0.5, "roll_amp": 1.3}),
    ]


_CAT2_TEMPLATES = [
    _tpl_cat2_collada, _tpl_cat2_tenerife, _tpl_cat2_jagged, _tpl_cat2_stepped,
]


def cat2_ramps(total_km: float, seed: int) -> ArchetypeOutput:
    """Cat-2 stepped ramps. 4 templates."""
    t = _pick_template(seed, len(_CAT2_TEMPLATES))
    lead = _lead_in(seed, probability=0.30)
    sections = _CAT2_TEMPLATES[t](total_km, seed, lead)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "climb", "summit",
                     f"cat2_ramps_T{t}")


# ── cat1_sustained: CL_CAT1_ROCACORBA / CL_CAT1_LOS_LOROS / CL_CAT1_TEIDE ──

def _tpl_cat1_rocacorba(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Rocacorba — gentle approach, hard middle, -0.5% dip, hard finish."""
    core = total_km - sum(s.length_km for s in lead)
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in
         enumerate([0.30, 0.30, 0.05, 0.20, 0.05, 0.10])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("gradual_climb", core * p[0],
                {"avg": _jit_abs(seed, 1, 4.0, 5.0), "roll_amp": 1.3}),
        Section("sustained_climb", core * p[1],
                {"avg": _jit_abs(seed, 2, 9.5, 10.8), "roll_amp": 1.6}),
        Section("descent", core * p[2], {"grade": _jit_abs(seed, 3, -1.0, 0.0)}),
        Section("sustained_climb", core * p[3],
                {"avg": _jit_abs(seed, 4, 7.5, 8.5), "roll_amp": 1.3}),
        Section("plateau", core * p[4], {"grade": _jit_abs(seed, 5, 3.0, 4.0)}),
        Section("sustained_climb", core * p[5],
                {"avg": _jit_abs(seed, 6, 6.5, 7.5), "roll_amp": 1.0}),
    ]


def _tpl_cat1_los_loros(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Los Loros — warmup, sustained, relief, final hard (no taper)."""
    core = total_km - sum(s.length_km for s in lead)
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in
         enumerate([0.08, 0.30, 0.04, 0.45, 0.13])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("false_flat_up", core * p[0],
                {"grade": _jit_abs(seed, 1, 2.0, 3.5)}),
        Section("sustained_climb", core * p[1],
                {"avg": _jit_abs(seed, 2, 6.5, 7.5), "roll_amp": 1.5}),
        Section("plateau", core * p[2], {"grade": _jit_abs(seed, 3, 3.0, 4.0)}),
        Section("sustained_climb", core * p[3],
                {"avg": _jit_abs(seed, 4, 7.5, 8.5), "roll_amp": 1.6}),
        Section("sustained_climb", core * p[4],
                {"avg": _jit_abs(seed, 5, 7.5, 8.5), "roll_amp": 1.3}),
    ]


def _tpl_cat1_teide_vilaflor(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Teide Vilaflor — oscillating trend with one descent."""
    core = total_km - sum(s.length_km for s in lead)
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in
         enumerate([0.08, 0.22, 0.05, 0.22, 0.04, 0.30, 0.09])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("false_flat_up", core * p[0], {"grade": _jit_abs(seed, 1, 2.0, 3.0)}),
        Section("sustained_climb", core * p[1],
                {"avg": _jit_abs(seed, 2, 5.5, 6.5), "roll_amp": 1.4}),
        Section("plateau", core * p[2], {"grade": _jit_abs(seed, 3, 3.0, 4.0)}),
        Section("sustained_climb", core * p[3],
                {"avg": _jit_abs(seed, 4, 8.5, 9.5), "roll_amp": 1.5}),
        Section("plateau", core * p[4], {"grade": _jit_abs(seed, 5, 3.0, 4.0)}),
        Section("sustained_climb", core * p[5],
                {"avg": _jit_abs(seed, 6, 7.5, 8.5), "roll_amp": 1.4}),
        Section("descent", core * p[6], {"grade": _jit_abs(seed, 7, -2.0, -1.0)}),
    ]


def _tpl_cat1_axamer_metronome(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Axamer — metronomic climb, very low SD."""
    core = total_km - sum(s.length_km for s in lead)
    warm = min(1.0, core * 0.08)
    return lead + [
        Section("false_flat_up", warm, {"grade": _jit_abs(seed, 1, 2.0, 3.0)}),
        Section("sustained_climb", core - warm,
                {"avg": _jit_abs(seed, 2, 8.5, 9.5),
                 "roll_amp": _jit_abs(seed, 3, 0.6, 0.9)}),
    ]


_CAT1_SUSTAINED_TEMPLATES = [
    _tpl_cat1_rocacorba, _tpl_cat1_los_loros,
    _tpl_cat1_teide_vilaflor, _tpl_cat1_axamer_metronome,
]


def cat1_sustained(total_km: float, seed: int) -> ArchetypeOutput:
    """Cat-1 sustained alpine. 4 templates with 60% lead-in rate."""
    t = _pick_template(seed, len(_CAT1_SUSTAINED_TEMPLATES))
    lead = _lead_in(seed, probability=0.50)
    sections = _CAT1_SUSTAINED_TEMPLATES[t](total_km, seed, lead)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "climb", "summit",
                     f"cat1_sustained_T{t}")


# ── cat1_variable: CL_CAT1_COLLADA_VARIABLE / CL_CAT1_SPIKED /
#                    CL_CAT1_GALIBIER / CL_CAT1_MID_DIP ──

def _tpl_cat1_collada_variable(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Jagged spike-relief pattern, different spacing each kicker."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 5.5, 7.0)
    n = max(3, int(core / _jit_abs(seed, 2, 2.0, 3.0)))
    climb_lens = _split_poisson(core * 0.72, n, seed + 1501, min_frac=0.4)
    kick_lens = _split_poisson(core * 0.28, n, seed + 1502, min_frac=0.3)
    sections = list(lead)
    for k in range(n):
        sections.append(Section("sustained_climb", climb_lens[k],
                                {"avg": avg + _jit_abs(seed, 100 + k, -0.5, 1.5),
                                 "roll_amp": 1.5}))
        sections.append(Section("kicker_up", kick_lens[k],
                                {"peak": avg + _jit_abs(seed, 200 + k, 4.0, 6.0)}))
    return sections


def _tpl_cat1_galibier(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Galibier — two big spikes separated by sustained, taper at end."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 5.5, 7.0)
    p = [_jit(seed, 100 + i, b, 0.15) for i, b in
         enumerate([0.20, 0.05, 0.45, 0.05, 0.25])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("gradual_climb", core * p[0],
                {"avg": avg - 1.0, "roll_amp": 1.0}),
        Section("kicker_up", core * p[1], {"peak": avg + 5.0}),
        Section("sustained_climb", core * p[2],
                {"avg": avg, "roll_amp": 1.4}),
        Section("kicker_up", core * p[3], {"peak": avg + 6.0}),
        Section("sustained_climb", core * p[4],
                {"avg": avg + 0.5, "roll_amp": 1.2}),
    ]


def _tpl_cat1_mid_dip(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Mid-climb true descent — rare but real."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 5.5, 7.0)
    p = [_jit(seed, 100 + i, b, 0.15) for i, b in
         enumerate([0.30, 0.05, 0.30, 0.05, 0.30])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("sustained_climb", core * p[0],
                {"avg": avg + 0.5, "roll_amp": 1.4}),
        Section("descent", core * p[1], {"grade": _jit_abs(seed, 2, -1.5, -0.5)}),
        Section("sustained_climb", core * p[2],
                {"avg": avg + 1.0, "roll_amp": 1.5}),
        Section("plateau", core * p[3], {"grade": _jit_abs(seed, 3, 2.5, 3.5)}),
        Section("sustained_climb", core * p[4],
                {"avg": avg + 1.5, "roll_amp": 1.6}),
    ]


def _tpl_cat1_spiked(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Spiked — long sustained with jagged spikes at Poisson intervals."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 5.5, 7.0)
    # Poisson-spaced 3-4 kickers, surrounded by sustained
    n = 3 + int(seeded_random(seed, 2) * 2)
    kick_lens = [_jit_abs(seed, 100 + i, 0.2, 0.4) for i in range(n)]
    between_lens = _split_poisson(core - sum(kick_lens), n + 1, seed + 1503,
                                  min_frac=0.4)
    sections = list(lead)
    sections.append(Section("sustained_climb", between_lens[0],
                            {"avg": avg, "roll_amp": 1.5}))
    for k in range(n):
        sections.append(Section("kicker_up", kick_lens[k],
                                {"peak": _jit_abs(seed, 200 + k, avg + 4, avg + 6)}))
        sections.append(Section("sustained_climb", between_lens[k + 1],
                                {"avg": avg + _jit_abs(seed, 300 + k, -0.5, 1.0),
                                 "roll_amp": 1.4}))
    return sections


_CAT1_VARIABLE_TEMPLATES = [
    _tpl_cat1_collada_variable, _tpl_cat1_galibier,
    _tpl_cat1_mid_dip, _tpl_cat1_spiked,
]


def cat1_variable(total_km: float, seed: int) -> ArchetypeOutput:
    """Cat-1 variable climb. 4 templates — jagged spikes."""
    t = _pick_template(seed, len(_CAT1_VARIABLE_TEMPLATES))
    lead = _lead_in(seed, probability=0.30)
    sections = _CAT1_VARIABLE_TEMPLATES[t](total_km, seed, lead)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "climb", "summit",
                     f"cat1_variable_T{t}")


# ── hc_steady: CL_HC_TEIDE_CABLECAR / CL_HC_AXAMER / CL_HC_ALPE_CLIMB ──
# All must have ≥2 plateau sections (Gavia signature required).

def _tpl_hc_teide_cablecar(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Teide Cable Car — long gentle metronome with 2 plateau reliefs + taper."""
    core = total_km - sum(s.length_km for s in lead)
    # Plateaus >=10% core so the low-grade interior survives ease-in blending
    p = [_jit(seed, 100 + i, b, 0.08) for i, b in
         enumerate([0.12, 0.30, 0.11, 0.18, 0.10, 0.19])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("false_flat_up", core * p[0], {"grade": _jit_abs(seed, 1, 3.0, 4.0)}),
        Section("gradual_climb", core * p[1],
                {"avg": _jit_abs(seed, 2, 5.5, 6.5), "roll_amp": 1.3}),
        Section("plateau", core * p[2], {"grade": _jit_abs(seed, 3, 2.0, 3.0)}),
        Section("sustained_climb", core * p[3],
                {"avg": _jit_abs(seed, 4, 6.5, 7.5), "roll_amp": 1.3}),
        Section("plateau", core * p[4], {"grade": _jit_abs(seed, 5, 1.5, 2.5)}),
        Section("gradual_climb", core * p[5],
                {"avg": _jit_abs(seed, 6, 2.5, 3.5), "roll_amp": 1.0}),
    ]


def _tpl_hc_axamer_metronome(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Axamer metronomic — two wider plateau reliefs to sustain HC signature."""
    core = total_km - sum(s.length_km for s in lead)
    # Plateaus must be wide enough (>=7% core) to survive ease-in/out blending.
    p = [_jit(seed, 100 + i, b, 0.08) for i, b in
         enumerate([0.28, 0.08, 0.28, 0.08, 0.28])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("sustained_climb", core * p[0],
                {"avg": _jit_abs(seed, 1, 8.0, 9.0), "roll_amp": 0.7}),
        Section("plateau", core * p[1], {"grade": _jit_abs(seed, 2, 2.5, 3.5)}),
        Section("sustained_climb", core * p[2],
                {"avg": _jit_abs(seed, 3, 9.0, 10.0), "roll_amp": 0.8}),
        Section("plateau", core * p[3], {"grade": _jit_abs(seed, 4, 2.5, 3.5)}),
        Section("sustained_climb", core * p[4],
                {"avg": _jit_abs(seed, 5, 7.5, 8.5), "roll_amp": 0.8}),
    ]


def _tpl_hc_alpe_climb(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Alpe — sustained throughout, two wider plateau reliefs (hairpins)."""
    core = total_km - sum(s.length_km for s in lead)
    # Plateaus widened to 8% core so low-grade interior survives smoothing.
    p = [_jit(seed, 100 + i, b, 0.10) for i, b in
         enumerate([0.10, 0.28, 0.08, 0.28, 0.08, 0.18])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("false_flat_up", core * p[0], {"grade": _jit_abs(seed, 1, 4.5, 5.5)}),
        Section("sustained_climb", core * p[1],
                {"avg": _jit_abs(seed, 2, 8.5, 9.5), "roll_amp": 1.4}),
        Section("plateau", core * p[2], {"grade": _jit_abs(seed, 3, 3.0, 4.0)}),
        Section("sustained_climb", core * p[3],
                {"avg": _jit_abs(seed, 4, 8.5, 9.5), "roll_amp": 1.4}),
        Section("plateau", core * p[4], {"grade": _jit_abs(seed, 5, 3.0, 4.0)}),
        Section("sustained_climb", core * p[5],
                {"avg": _jit_abs(seed, 6, 9.0, 10.5), "roll_amp": 1.5}),
    ]


def _tpl_hc_gavia_signature(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Canonical Gavia — two dramatic relief plateaus."""
    core = total_km - sum(s.length_km for s in lead)
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in
         enumerate([0.12, 0.10, 0.22, 0.08, 0.32, 0.16])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("gradual_climb", core * p[0],
                {"avg": _jit_abs(seed, 1, 4.5, 5.5), "roll_amp": 1.2}),
        Section("plateau", core * p[1], {"grade": _jit_abs(seed, 2, 1.5, 2.5)}),
        Section("sustained_climb", core * p[2],
                {"avg": _jit_abs(seed, 3, 6.5, 7.5), "roll_amp": 1.5}),
        Section("plateau", core * p[3], {"grade": _jit_abs(seed, 4, 2.0, 3.0)}),
        Section("sustained_climb", core * p[4],
                {"avg": _jit_abs(seed, 5, 7.5, 8.5), "roll_amp": 1.6}),
        Section("gradual_climb", core * p[5],
                {"avg": _jit_abs(seed, 6, 3.5, 4.5), "roll_amp": 1.0}),
    ]


_HC_STEADY_TEMPLATES = [
    _tpl_hc_teide_cablecar, _tpl_hc_axamer_metronome,
    _tpl_hc_alpe_climb, _tpl_hc_gavia_signature,
]


def hc_steady(total_km: float, seed: int) -> ArchetypeOutput:
    """HC steady. 4 templates, ≥2 plateau sections required."""
    t = _pick_template(seed, len(_HC_STEADY_TEMPLATES))
    lead = _lead_in(seed, probability=0.50)
    sections = _HC_STEADY_TEMPLATES[t](total_km, seed, lead)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "climb", "summit",
                     f"hc_steady_T{t}")


# ── hc_irregular: CL_HC_GAVIA_RAMP_PLATEAU / CL_HC_VENTOUX_MALAUCENE /
#                  CL_HC_MORTIROLO_STYLE — with ≥2 plateaus each ──

def _tpl_hc_gavia_ramp_plateau(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Gavia ramp-plateau-ramp-plateau-final signature. One final spike >=13%."""
    core = total_km - sum(s.length_km for s in lead)
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in
         enumerate([0.08, 0.12, 0.20, 0.10, 0.32, 0.05, 0.13])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("gradual_climb", core * p[0],
                {"avg": _jit_abs(seed, 1, 4.5, 6.0), "roll_amp": 1.2}),
        Section("plateau", core * p[1], {"grade": _jit_abs(seed, 2, 0.5, 2.0)}),
        Section("sustained_climb", core * p[2],
                {"avg": _jit_abs(seed, 3, 6.5, 8.0), "roll_amp": 1.5}),
        Section("plateau", core * p[3], {"grade": _jit_abs(seed, 4, 2.0, 3.5)}),
        Section("sustained_climb", core * p[4],
                {"avg": _jit_abs(seed, 5, 7.5, 9.0), "roll_amp": 1.6}),
        Section("kicker_up", core * p[5],
                {"peak": _jit_abs(seed, 8, 13.5, 15.5)}),
        Section("gradual_climb", core * p[6],
                {"avg": _jit_abs(seed, 6, 3.0, 5.0), "roll_amp": 1.0}),
    ]


def _tpl_hc_ventoux_malaucene(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Ventoux — sustained, mid-dip, hard, relief, final kick (>=13%)."""
    core = total_km - sum(s.length_km for s in lead)
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in
         enumerate([0.10, 0.20, 0.10, 0.20, 0.10, 0.15, 0.05, 0.10])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("gradual_climb", core * p[0],
                {"avg": _jit_abs(seed, 1, 6.5, 7.5), "roll_amp": 1.4}),
        Section("sustained_climb", core * p[1],
                {"avg": _jit_abs(seed, 2, 7.5, 9.0), "roll_amp": 1.5}),
        Section("plateau", core * p[2], {"grade": _jit_abs(seed, 3, 2.5, 3.5)}),
        Section("sustained_climb", core * p[3],
                {"avg": _jit_abs(seed, 4, 9.0, 10.5), "roll_amp": 1.6}),
        Section("plateau", core * p[4], {"grade": _jit_abs(seed, 5, 3.5, 5.0)}),
        Section("sustained_climb", core * p[5],
                {"avg": _jit_abs(seed, 6, 8.5, 10.5), "roll_amp": 1.6}),
        Section("kicker_up", core * p[6],
                {"peak": _jit_abs(seed, 8, 13.5, 15.0)}),
        Section("gradual_climb", core * p[7],
                {"avg": _jit_abs(seed, 7, 2.5, 4.0), "roll_amp": 1.0}),
    ]


def _tpl_hc_mortirolo(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Mortirolo — relentless kickers with short plateaus between."""
    core = total_km - sum(s.length_km for s in lead)
    n = 3 + int(seeded_random(seed, 1) * 2)
    sections = list(lead)
    # Split total: 60% climbing, 25% kickers, 15% plateaus
    climb_lens = _split_poisson(core * 0.55, n, seed + 1601, min_frac=0.45)
    kick_lens = _split_poisson(core * 0.20, n, seed + 1602, min_frac=0.3)
    plat_lens = _split_poisson(core * 0.25, n, seed + 1603, min_frac=0.3)
    for k in range(n):
        sections.append(Section("sustained_climb", climb_lens[k],
                                {"avg": _jit_abs(seed, 100 + k, 8.0, 9.5),
                                 "roll_amp": 1.7}))
        sections.append(Section("kicker_up", kick_lens[k],
                                {"peak": _jit_abs(seed, 200 + k, 13.5, 16.0)}))
        sections.append(Section("plateau", plat_lens[k],
                                {"grade": _jit_abs(seed, 300 + k, 2.5, 4.0)}))
    return sections


def _tpl_hc_asymmetric(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Asymmetric — hardest kickers placed in last third."""
    core = total_km - sum(s.length_km for s in lead)
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in
         enumerate([0.15, 0.20, 0.06, 0.15, 0.05, 0.06, 0.15, 0.05, 0.13])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("gradual_climb", core * p[0],
                {"avg": _jit_abs(seed, 1, 5.5, 6.5), "roll_amp": 1.3}),
        Section("sustained_climb", core * p[1],
                {"avg": _jit_abs(seed, 2, 7.5, 8.5), "roll_amp": 1.6}),
        Section("plateau", core * p[2], {"grade": _jit_abs(seed, 3, 3.0, 4.0)}),
        Section("sustained_climb", core * p[3],
                {"avg": _jit_abs(seed, 4, 8.5, 9.5), "roll_amp": 1.6}),
        Section("kicker_up", core * p[4],
                {"peak": _jit_abs(seed, 5, 13.5, 14.5)}),
        Section("plateau", core * p[5], {"grade": _jit_abs(seed, 6, 2.5, 3.5)}),
        Section("sustained_climb", core * p[6],
                {"avg": _jit_abs(seed, 7, 9.0, 10.0), "roll_amp": 1.6}),
        Section("kicker_up", core * p[7],
                {"peak": _jit_abs(seed, 8, 15.0, 16.5)}),
        Section("sustained_climb", core * p[8],
                {"avg": _jit_abs(seed, 9, 8.5, 9.5), "roll_amp": 1.5}),
    ]


_HC_IRREGULAR_TEMPLATES = [
    _tpl_hc_gavia_ramp_plateau, _tpl_hc_ventoux_malaucene,
    _tpl_hc_mortirolo, _tpl_hc_asymmetric,
]


def hc_irregular(total_km: float, seed: int) -> ArchetypeOutput:
    """HC irregular. 4 templates. Each has ≥2 plateau sections."""
    t = _pick_template(seed, len(_HC_IRREGULAR_TEMPLATES))
    lead = _lead_in(seed, probability=0.40)
    sections = _HC_IRREGULAR_TEMPLATES[t](total_km, seed, lead)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "climb", "summit",
                     f"hc_irregular_T{t}")


# ── two_stepper: CL_TWO_STEPPER_CLIMB_VALLEY_CLIMB / CL_TWO_STEPPER_MIRROR ──

def _tpl_ts_falzarego(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Falzarego — short first climb, valley, bigger second climb."""
    core = total_km - sum(s.length_km for s in lead)
    p = [_jit(seed, 100 + i, b, 0.14) for i, b in
         enumerate([0.30, 0.20, 0.06, 0.44])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("gradual_climb", core * p[0],
                {"avg": _jit_abs(seed, 1, 5.5, 6.5), "roll_amp": 1.3}),
        Section("descent", core * p[1], {"grade": _jit_abs(seed, 2, -3.5, -2.5)}),
        Section("false_flat_up", core * p[2],
                {"grade": _jit_abs(seed, 3, 1.5, 2.5)}),
        Section("sustained_climb", core * p[3],
                {"avg": _jit_abs(seed, 4, 7.0, 8.0), "roll_amp": 1.5}),
    ]


def _tpl_ts_mirror(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Mirror — symmetric but slightly different characters."""
    core = total_km - sum(s.length_km for s in lead)
    climb1 = _jit(seed, 1, 0.40, 0.15)
    rest = 1.0 - climb1
    descent = rest * _jit(seed, 2, 0.40, 0.15)
    climb2 = rest - descent
    return lead + [
        Section("gradual_climb", core * climb1,
                {"avg": _jit_abs(seed, 3, 5.5, 7.0), "roll_amp": 1.3}),
        Section("descent", core * descent,
                {"grade": _jit_abs(seed, 4, -4.5, -3.0)}),
        Section("sustained_climb", core * climb2,
                {"avg": _jit_abs(seed, 5, 6.0, 7.5), "roll_amp": 1.4}),
    ]


def _tpl_ts_asymmetric(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Small first, long second."""
    core = total_km - sum(s.length_km for s in lead)
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in
         enumerate([0.20, 0.10, 0.05, 0.65])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("gradual_climb", core * p[0],
                {"avg": _jit_abs(seed, 1, 4.5, 5.5), "roll_amp": 1.1}),
        Section("descent", core * p[1], {"grade": _jit_abs(seed, 2, -5.0, -4.0)}),
        Section("flat", core * p[2], {"grade": _jit_abs(seed, 3, 0.0, 0.8)}),
        Section("sustained_climb", core * p[3],
                {"avg": _jit_abs(seed, 4, 7.0, 8.0), "roll_amp": 1.5}),
    ]


_TWO_STEPPER_TEMPLATES = [
    _tpl_ts_falzarego, _tpl_ts_mirror, _tpl_ts_asymmetric,
]


def two_stepper(total_km: float, seed: int) -> ArchetypeOutput:
    """Two-stepper climb-valley-climb. 3 templates."""
    t = _pick_template(seed, len(_TWO_STEPPER_TEMPLATES))
    lead = _lead_in(seed, probability=0.30)
    sections = _TWO_STEPPER_TEMPLATES[t](total_km, seed, lead)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "climb", "summit",
                     f"two_stepper_T{t}")


# ── summit_sprint: CL_SUMMIT_SPRINT_MUR_DE_BRETAGNE / CL_SUMMIT_SPRINT_INNSBRUCK ──

def _tpl_ss_mur_de_bretagne(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Mûr de Bretagne — gradual climb + final kicker."""
    core = total_km - sum(s.length_km for s in lead)
    kick_frac = _jit(seed, 1, 0.15, 0.2)
    avg = _jit_abs(seed, 2, 4.5, 6.0)
    return lead + [
        Section("gradual_climb", core * (1 - kick_frac),
                {"avg": avg, "roll_amp": 1.2}),
        Section("kicker_up", core * kick_frac,
                {"peak": _jit_abs(seed, 3, 11.0, 13.0)}),
    ]


def _tpl_ss_innsbruck(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Innsbruck — rolling tempo approach + final wall."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 4.5, 6.0)
    kick_frac = _jit(seed, 2, 0.12, 0.25)
    tempo_frac = _jit(seed, 3, 0.38, 0.15)
    roll_frac = 1 - kick_frac - tempo_frac
    return lead + [
        Section("rolling", core * roll_frac,
                {"baseline": avg,
                 "amp": _jit_abs(seed, 4, 0.8, 1.4),
                 "period_km": _jit_abs(seed, 5, 0.8, 1.3)}),
        Section("sustained_climb", core * tempo_frac,
                {"avg": avg + 1.0, "roll_amp": 1.2}),
        Section("kicker_up", core * kick_frac,
                {"peak": _jit_abs(seed, 6, 12.0, 14.0)}),
    ]


def _tpl_ss_double_kick(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Double kicker — two spikes, plateau between."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 4.5, 6.0)
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in
         enumerate([0.60, 0.10, 0.15, 0.15])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("gradual_climb", core * p[0], {"avg": avg, "roll_amp": 1.2}),
        Section("kicker_up", core * p[1], {"peak": _jit_abs(seed, 2, 8.5, 9.5)}),
        Section("plateau", core * p[2], {"grade": _jit_abs(seed, 3, 2.5, 3.5)}),
        Section("kicker_up", core * p[3], {"peak": _jit_abs(seed, 4, 11.5, 13.5)}),
    ]


_SUMMIT_SPRINT_TEMPLATES = [
    _tpl_ss_mur_de_bretagne, _tpl_ss_innsbruck, _tpl_ss_double_kick,
]


def summit_sprint(total_km: float, seed: int) -> ArchetypeOutput:
    """Mid-length climb + final kicker. 3 templates."""
    t = _pick_template(seed, len(_SUMMIT_SPRINT_TEMPLATES))
    lead = _lead_in(seed, probability=0.30)
    sections = _SUMMIT_SPRINT_TEMPLATES[t](total_km, seed, lead)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "climb", "wall",
                     f"summit_sprint_T{t}")


# ── false_flat_climb: CL_FALSE_FLAT_TEMPUS_TO_CASSINE / CL_FALSE_FLAT_GENTLE_DRIFT ──

def _tpl_ff_tempus_to_cassine(total_km: float, seed: int) -> list[Section]:
    """Pure false flat."""
    return [Section("false_flat_up", total_km,
                    {"grade": _jit_abs(seed, 1, 2.0, 3.3)})]


def _tpl_ff_gentle_drift(total_km: float, seed: int) -> list[Section]:
    """Gentle drift — two slightly different false-flat grades."""
    lens = _split_poisson(total_km, 2, seed + 1701, min_frac=0.6)
    base = _jit_abs(seed, 1, 2.0, 3.0)
    return [
        Section("false_flat_up", lens[0],
                {"grade": base - _jit_abs(seed, 2, 0.2, 0.5)}),
        Section("false_flat_up", lens[1],
                {"grade": base + _jit_abs(seed, 3, 0.2, 0.5)}),
    ]


def _tpl_ff_flat_edges(total_km: float, seed: int) -> list[Section]:
    """Flat bookends with false flat in middle."""
    edge_frac = _jit(seed, 1, 0.10, 0.3)
    flat_edge = total_km * edge_frac
    middle = total_km - 2 * flat_edge
    return [
        Section("flat", flat_edge, {"grade": _jit_abs(seed, 2, 0.2, 0.5)}),
        Section("false_flat_up", middle, {"grade": _jit_abs(seed, 3, 2.2, 3.2)}),
        Section("flat", flat_edge, {"grade": _jit_abs(seed, 4, 0.1, 0.4)}),
    ]


_FALSE_FLAT_TEMPLATES = [
    _tpl_ff_tempus_to_cassine, _tpl_ff_gentle_drift, _tpl_ff_flat_edges,
]


def false_flat_climb(total_km: float, seed: int) -> ArchetypeOutput:
    """False-flat climb. 3 templates."""
    t = _pick_template(seed, len(_FALSE_FLAT_TEMPLATES))
    sections = _FALSE_FLAT_TEMPLATES[t](total_km, seed)
    surf = uniform_surface(total_km, "asphalt")
    return _finalize(sections, seed, total_km, surf, "climb", "summit",
                     f"false_flat_climb_T{t}")


# ── COBBLE FAMILY (4) ────────────────────────────────────────────────────────

# cobble_flat_classic: CB_ROUBAIX_CLASSIC / CB_RICHMOND_CITY_LAPS

def _tpl_cb_roubaix_classic(total_km: float, seed: int) -> list[Section]:
    """CB_ROUBAIX_CLASSIC — flat long, roughness provides the difficulty."""
    lens = _split_poisson(total_km, 3, seed + 1801, min_frac=0.5)
    return [
        Section("flat", lens[0], {"grade": _jit_abs(seed, 1, 0.0, 0.3)}),
        Section("false_flat_up", lens[1], {"grade": _jit_abs(seed, 2, 0.3, 0.7)}),
        Section("flat", lens[2], {"grade": _jit_abs(seed, 3, -0.2, 0.2)}),
    ]


def _tpl_cb_richmond_city(total_km: float, seed: int) -> list[Section]:
    """CB_RICHMOND_CITY_LAPS — flat with aperiodic roll undulations."""
    return [Section("rolling", total_km,
                    {"baseline": _jit_abs(seed, 1, 0.1, 0.3),
                     "amp": _jit_abs(seed, 2, 0.8, 1.2),
                     "period_km": _jit_abs(seed, 3, 3.5, 4.5)})]


def _tpl_cb_roubaix_gentle_roll(total_km: float, seed: int) -> list[Section]:
    """Half flat, half gentle rolling."""
    lens = _split_poisson(total_km, 2, seed + 1802, min_frac=0.6)
    return [
        Section("flat", lens[0], {"grade": _jit_abs(seed, 1, 0.0, 0.3)}),
        Section("rolling", lens[1],
                {"baseline": _jit_abs(seed, 2, 0.1, 0.3),
                 "amp": _jit_abs(seed, 3, 0.7, 1.1),
                 "period_km": _jit_abs(seed, 4, 2.5, 3.5)}),
    ]


_COBBLE_FLAT_TEMPLATES = [
    _tpl_cb_roubaix_classic, _tpl_cb_richmond_city, _tpl_cb_roubaix_gentle_roll,
]


def cobble_flat_classic(total_km: float, seed: int) -> ArchetypeOutput:
    """Paris-Roubaix flat + cobble sectors. 3 templates."""
    t = _pick_template(seed, len(_COBBLE_FLAT_TEMPLATES))
    sections = _COBBLE_FLAT_TEMPLATES[t](total_km, seed)
    sector_count = 5 + int(seeded_random(seed, 170) * 6)
    surf = scatter_cobble_sectors(total_km, seed, sector_count,
                                  sector_min_km=0.5, sector_max_km=2.0)
    return _finalize(sections, seed, total_km, surf, "flat", "none",
                     f"cobble_flat_classic_T{t}")


# cobble_climb_muur: CB_MUUR / CB_KOPPENBERG / CB_PATERBERG

def _tpl_cb_muur(total_km: float, seed: int) -> list[Section]:
    """Muur — 3-4% transitions to 10-12%, stepped cobble roughness."""
    warm = _jit_abs(seed, 1, 0.08, 0.14)
    mid = total_km - warm
    lens = _split_poisson(mid, 2, seed + 1901, min_frac=0.5)
    return [
        Section("false_flat_up", warm, {"grade": _jit_abs(seed, 2, 3.0, 4.2)}),
        Section("steep_wall", lens[0], {"avg": _jit_abs(seed, 3, 8.5, 10.5)}),
        Section("steep_wall", lens[1], {"avg": _jit_abs(seed, 4, 10.5, 12.5)}),
    ]


def _tpl_cb_koppenberg(total_km: float, seed: int) -> list[Section]:
    """Koppenberg — ramp quickly to 20%+."""
    warm = _jit_abs(seed, 1, 0.10, 0.18)
    return [
        Section("false_flat_up", warm, {"grade": _jit_abs(seed, 2, 4.0, 6.0)}),
        Section("steep_wall", total_km - warm,
                {"avg": _jit_abs(seed, 3, 12.0, 14.5)}),
    ]


def _tpl_cb_paterberg(total_km: float, seed: int) -> list[Section]:
    """Paterberg — monotonic steep throughout."""
    warm = _jit_abs(seed, 1, 0.05, 0.10)
    return [
        Section("flat", warm, {"grade": _jit_abs(seed, 2, 1.5, 2.5)}),
        Section("steep_wall", total_km - warm,
                {"avg": _jit_abs(seed, 3, 10.5, 12.0)}),
    ]


_COBBLE_MUUR_TEMPLATES = [
    _tpl_cb_muur, _tpl_cb_koppenberg, _tpl_cb_paterberg,
]


def cobble_climb_muur(total_km: float, seed: int) -> ArchetypeOutput:
    """Muur cobble wall. 3 templates."""
    t = _pick_template(seed, len(_COBBLE_MUUR_TEMPLATES))
    sections = _COBBLE_MUUR_TEMPLATES[t](total_km, seed)
    sections = _scale_to(total_km, sections)
    surf = uniform_surface(total_km, "cobble")
    return _finalize(sections, seed, total_km, surf, "climb", "wall",
                     f"cobble_climb_muur_T{t}")


# cobble_rolling: CB_FLEMISH_COUNTRYSIDE / CB_BOLOGNA_CITY_MIX

def _tpl_cb_flemish_countryside(total_km: float, seed: int) -> list[Section]:
    """Flemish — rolling with 3-4 aperiodic short hills."""
    intro = _jit_abs(seed, 1, 0.8, 1.2)
    remain = total_km - intro
    n = 3 + int(seeded_random(seed, 2) * 2)
    hill_ws = [_jit_abs(seed, 100 + i, 0.4, 0.7) for i in range(n)]
    roll_lens = _split_poisson(remain - sum(hill_ws), n + 1, seed + 2001,
                               min_frac=0.3)
    sections = [Section("flat", intro, {"grade": _jit_abs(seed, 3, 0.1, 0.3)})]
    sections.append(Section("rolling", roll_lens[0],
                            {"baseline": _jit_abs(seed, 4, 0.3, 0.7),
                             "amp": _jit_abs(seed, 5, 2.2, 2.8),
                             "period_km": _jit_abs(seed, 6, 1.6, 2.2)}))
    for k in range(n):
        sections.append(Section("short_hill", hill_ws[k],
                                {"peak": _jit_abs(seed, 200 + k, 6.5, 8.5)}))
        sections.append(Section("rolling", roll_lens[k + 1],
                                {"baseline": _jit_abs(seed, 300 + k, 0.2, 0.5),
                                 "amp": _jit_abs(seed, 400 + k, 2.0, 2.5),
                                 "period_km": _jit_abs(seed, 500 + k, 1.5, 2.2)}))
    return sections


def _tpl_cb_bologna_city_mix(total_km: float, seed: int) -> list[Section]:
    """Bologna city mix — long rolling, quieter."""
    lens = _split_poisson(total_km, 2, seed + 2002, min_frac=0.5)
    return [
        Section("rolling", lens[0],
                {"baseline": _jit_abs(seed, 1, 0.3, 0.6),
                 "amp": _jit_abs(seed, 2, 2.5, 3.2),
                 "period_km": _jit_abs(seed, 3, 1.8, 2.4)}),
        Section("rolling", lens[1],
                {"baseline": _jit_abs(seed, 4, 0.2, 0.5),
                 "amp": _jit_abs(seed, 5, 2.2, 2.8),
                 "period_km": _jit_abs(seed, 6, 2.0, 2.6)}),
    ]


def _tpl_cb_classics_with_berg(total_km: float, seed: int) -> list[Section]:
    """Rolling + single berg + rolling tail."""
    berg = _jit_abs(seed, 1, 0.7, 1.0)
    tail = _jit_abs(seed, 2, 4.0, 6.0)
    body = total_km - berg - tail
    return [
        Section("rolling", body,
                {"baseline": _jit_abs(seed, 3, 0.3, 0.6),
                 "amp": _jit_abs(seed, 4, 2.0, 2.5),
                 "period_km": _jit_abs(seed, 5, 1.7, 2.3)}),
        Section("short_hill", berg, {"peak": _jit_abs(seed, 6, 7.5, 9.0)}),
        Section("rolling", tail,
                {"baseline": _jit_abs(seed, 7, 0.1, 0.4),
                 "amp": _jit_abs(seed, 8, 2.0, 2.4),
                 "period_km": _jit_abs(seed, 9, 1.8, 2.4)}),
    ]


def _tpl_cb_flanders_classic(total_km: float, seed: int) -> list[Section]:
    """Authentic Tour of Flanders DNA: 60-70% FLAT polder sections + 5-8
    SHORT STEEP hellingen (12-20% peaks) + 1-2 flat cobble sectors.

    Matches the real courses built in courses/flanders/ (Ronde van
    Vlaanderen, Omloop Het Nieuwsblad, Dwars door Vlaanderen,
    Kuurne-Brussel-Kuurne). Low per-km climb rate (3-6 m/km) but
    high peak grades — exactly like the real classics.
    """
    # 5-8 hellingen distributed aperiodically
    n_hellingen = 5 + int(seeded_random(seed, 100) * 4)
    # 1-2 flat cobble sectors (Haaghoek, Paddestraat style)
    n_sectors = 1 + int(seeded_random(seed, 101) * 2)
    # Helling widths — short and steep
    hill_ws = [_jit_abs(seed, 200 + i, 0.35, 1.1) for i in range(n_hellingen)]
    # Flat cobble sector widths — long and flat
    sector_ws = [_jit_abs(seed, 300 + i, 1.3, 2.5) for i in range(n_sectors)]

    total_features = sum(hill_ws) + sum(sector_ws)
    flat_budget = total_km - total_features
    # Distribute flat budget across (n_hellingen + n_sectors + 1) gaps
    n_gaps = n_hellingen + n_sectors + 1
    flat_lens = _split_poisson(flat_budget, n_gaps, seed + 4001,
                               min_frac=0.3)

    sections: list[Section] = []

    # Build a seeded interleave order: mostly flats with features sprinkled in,
    # ending on a long flat run-in (Flanders finish = flat ~20-30km)
    feature_positions = sorted(
        # First feature after at least ~15% of route
        max(0.12, seeded_random(seed, 500 + i) * 0.85)
        for i in range(n_hellingen + n_sectors)
    )
    # Run-in flat at the end — reserve 15-25% of total for finale
    runin_frac = 0.15 + seeded_random(seed, 501) * 0.10

    feature_list: list[tuple[str, float, dict]] = []
    for i in range(n_hellingen):
        feature_list.append(("helling", hill_ws[i], {}))
    for i in range(n_sectors):
        feature_list.append(("sector", sector_ws[i], {}))

    # Shuffle feature_list by seed
    idxs = list(range(len(feature_list)))
    for i in range(len(idxs) - 1, 0, -1):
        j = int(seeded_random(seed, 600 + i) * (i + 1))
        idxs[i], idxs[j] = idxs[j], idxs[i]
    feature_list = [feature_list[i] for i in idxs]

    # Build: flat, feature, descent, flat, feature, descent, ... flat (finale)
    feature_i = 0
    for i in range(n_gaps):
        # Flat before (or finale)
        is_finale = (i == n_gaps - 1)
        flat_len = flat_lens[i]
        if is_finale:
            # Make the final flat stretch longer — Flanders finish
            flat_len = max(flat_len, total_km * runin_frac)
        sections.append(Section("flat", flat_len,
                                {"grade": _jit_abs(seed, 700 + i, -0.1, 0.2)}))

        # Feature (if any left)
        if feature_i < len(feature_list):
            kind, ln, _ = feature_list[feature_i]
            if kind == "helling":
                # Short steep kicker — Flanders hellingen peaks 12-20%
                peak = _jit_abs(seed, 800 + feature_i, 12.5, 18.0)
                sections.append(Section("kicker_up", ln, {"peak": peak}))
                # Short descent after (1-1.5km)
                desc_len = _jit_abs(seed, 900 + feature_i, 0.8, 1.4)
                # Take descent out of NEXT flat's length
                if i + 1 < n_gaps:
                    flat_lens[i + 1] = max(0.3, flat_lens[i + 1] - desc_len)
                sections.append(Section("descent", desc_len,
                                        {"grade": _jit_abs(seed, 1000 + feature_i, -3.2, -2.2)}))
            else:  # flat cobble sector
                # Minimal grade change — just chunky flat pavé
                sections.append(Section("flat", ln,
                                        {"grade": _jit_abs(seed, 800 + feature_i, -0.2, 0.5)}))
            feature_i += 1

    return sections


_COBBLE_ROLLING_TEMPLATES = [
    _tpl_cb_flanders_classic,     # NEW — authentic Ronde van Vlaanderen DNA
    _tpl_cb_flanders_classic,     # 2x weight — this is the signature Flanders look
    _tpl_cb_flemish_countryside,  # rolling + short hills
    _tpl_cb_bologna_city_mix,
    _tpl_cb_classics_with_berg,
]


def cobble_rolling(total_km: float, seed: int) -> ArchetypeOutput:
    """Flemish rolling + cobble sectors. 4 templates, Flanders-weighted."""
    t = _pick_template(seed, len(_COBBLE_ROLLING_TEMPLATES))
    sections = _COBBLE_ROLLING_TEMPLATES[t](total_km, seed)
    # Cobble surface: place 4-8 cobble sectors over the asphalt base.
    # The flanders_classic template's hellingen are steep cobbled kickers,
    # the flat_cobble_sectors are Paddestraat/Haaghoek style long flat pavé.
    sector_count = 6 + int(seeded_random(seed, 200) * 5)
    surf = scatter_cobble_sectors(total_km, seed, sector_count,
                                  sector_min_km=0.3, sector_max_km=1.8)
    return _finalize(sections, seed, total_km, surf, "rolling", "none",
                     f"cobble_rolling_T{t}")


# cobble_finish: CB_BOLOGNA_TT / CB_RICHMOND_GOVERNOR_FINISH

def _tpl_cb_bologna_tt(total_km: float, seed: int) -> list[Section]:
    """Bologna TT — flat + sustained cobble kicker."""
    cobble_km = _jit_abs(seed, 1, 2.0, 3.0)
    body = total_km - cobble_km
    return [
        Section("flat", body, {"grade": _jit_abs(seed, 2, 0.0, 0.3)}),
        Section("sustained_climb", cobble_km,
                {"avg": _jit_abs(seed, 3, 5.5, 7.5), "roll_amp": 1.2}),
    ]


def _tpl_cb_richmond_governor(total_km: float, seed: int) -> list[Section]:
    """Richmond Governor — flat + false flat + kicker + short flat finish."""
    cobble_km = _jit_abs(seed, 1, 2.5, 3.5)
    tail_flat = _jit_abs(seed, 2, 0.2, 0.5)
    ff = _jit_abs(seed, 3, 0.5, 0.9)
    body = total_km - cobble_km - tail_flat - ff
    return [
        Section("flat", body, {"grade": _jit_abs(seed, 4, 0.0, 0.3)}),
        Section("false_flat_up", ff, {"grade": _jit_abs(seed, 5, 1.4, 2.2)}),
        Section("sustained_climb", cobble_km,
                {"avg": _jit_abs(seed, 6, 5.5, 7.5), "roll_amp": 1.3}),
        Section("flat", tail_flat, {"grade": _jit_abs(seed, 7, -0.3, 0.3)}),
    ]


def _tpl_cb_bologna_stepped(total_km: float, seed: int) -> list[Section]:
    """Body flat + gradual + stepped cobble finish with kicker."""
    cobble_km = _jit_abs(seed, 1, 2.5, 3.5)
    body = total_km - cobble_km
    body_lens = _split_poisson(body, 2, seed + 2101, min_frac=0.4)
    return [
        Section("flat", body_lens[0], {"grade": _jit_abs(seed, 2, 0.0, 0.3)}),
        Section("rolling", body_lens[1],
                {"baseline": _jit_abs(seed, 3, 0.2, 0.5),
                 "amp": _jit_abs(seed, 4, 0.8, 1.3),
                 "period_km": _jit_abs(seed, 5, 2.5, 3.5)}),
        Section("gradual_climb", cobble_km * 0.6,
                {"avg": _jit_abs(seed, 6, 5.0, 6.5), "roll_amp": 1.1}),
        Section("kicker_up", cobble_km * 0.4,
                {"peak": _jit_abs(seed, 7, 10.0, 12.5)}),
    ]


_COBBLE_FINISH_TEMPLATES = [
    _tpl_cb_bologna_tt, _tpl_cb_richmond_governor, _tpl_cb_bologna_stepped,
]


def cobble_finish(total_km: float, seed: int) -> ArchetypeOutput:
    """Bologna-style flat + cobble climb finish. 3 templates."""
    t = _pick_template(seed, len(_COBBLE_FINISH_TEMPLATES))
    sections = _COBBLE_FINISH_TEMPLATES[t](total_km, seed)
    # Mark the cobble portion as the last ~20% of the route.
    cobble_total = sum(s.length_km for s in sections[-2:])  # last 1-2 sections
    body_total = total_km - cobble_total
    surf = mixed_surface_segments(total_km, seed, [
        (0.0, body_total / total_km, "asphalt"),
        (body_total / total_km, 1.0, "cobble"),
    ])
    return _finalize(sections, seed, total_km, surf, "mixed", "wall",
                     f"cobble_finish_T{t}")


# ── GRAVEL FAMILY (5) ────────────────────────────────────────────────────────

# gravel_rolling_strade: GV_STRADE_BIANCHE / GV_DIRT_DEVOTEE / GV_ROAD_TO_RUINS

def _strade_hill_block(seed: int, total: float, n: int) -> list[Section]:
    """Build n Poisson-placed flat/hill/descent cells totalling ``total``."""
    # Split: 45% flat, 30% hill, 25% descent (per Strade Bianche character)
    hill_ws = [_jit_abs(seed, 100 + i, 0.55, 0.95) for i in range(n)]
    desc_ws = [_jit_abs(seed, 200 + i, 0.45, 0.80) for i in range(n)]
    used = sum(hill_ws) + sum(desc_ws)
    if used >= total * 0.85:
        # Shrink hills proportionally to leave 15% flat
        scale = total * 0.85 / used
        hill_ws = [w * scale for w in hill_ws]
        desc_ws = [w * scale for w in desc_ws]
        used = sum(hill_ws) + sum(desc_ws)
    flat_ws = _split_poisson(total - used, n + 1, seed + 2201, min_frac=0.35)
    sections: list[Section] = []
    for k in range(n):
        sections.append(Section("flat", flat_ws[k],
                                {"grade": _jit_abs(seed, 300 + k, 0.1, 0.4)}))
        sections.append(Section("short_hill", hill_ws[k],
                                {"peak": _jit_abs(seed, 400 + k, 6.5, 9.0)}))
        sections.append(Section("descent", desc_ws[k],
                                {"grade": _jit_abs(seed, 500 + k, -3.0, -1.8)}))
    sections.append(Section("flat", flat_ws[-1],
                            {"grade": _jit_abs(seed, 600, 0.0, 0.3)}))
    return sections


def _tpl_gv_strade_bianche(total_km: float, seed: int) -> list[Section]:
    """GV_STRADE_BIANCHE — 2 km flat lead + 4 Poisson-placed gravel kickers."""
    intro = _jit_abs(seed, 1, 1.5, 2.5)
    return [Section("flat", intro, {"grade": _jit_abs(seed, 2, 0.0, 0.3)})
            ] + _strade_hill_block(seed + 2202, total_km - intro, 4)


def _tpl_gv_dirt_devotee(total_km: float, seed: int) -> list[Section]:
    """GV_DIRT_DEVOTEE — 5 tighter kickers."""
    intro = _jit_abs(seed, 1, 1.0, 1.8)
    return [Section("flat", intro, {"grade": _jit_abs(seed, 2, 0.0, 0.3)})
            ] + _strade_hill_block(seed + 2203, total_km - intro, 5)


def _tpl_gv_road_to_ruins(total_km: float, seed: int) -> list[Section]:
    """GV_ROAD_TO_RUINS — 3 bigger kickers with longer flats."""
    intro = _jit_abs(seed, 1, 2.5, 3.5)
    return [Section("flat", intro, {"grade": _jit_abs(seed, 2, 0.0, 0.3)})
            ] + _strade_hill_block(seed + 2204, total_km - intro, 3)


_GV_ROLLING_STRADE_TEMPLATES = [
    _tpl_gv_strade_bianche, _tpl_gv_dirt_devotee, _tpl_gv_road_to_ruins,
]


def gravel_rolling_strade(total_km: float, seed: int) -> ArchetypeOutput:
    """Strade Bianche gravel rolling. 3 templates."""
    t = _pick_template(seed, len(_GV_ROLLING_STRADE_TEMPLATES))
    sections = _GV_ROLLING_STRADE_TEMPLATES[t](total_km, seed)
    # Asphalt+gravel surface mix (heavy gravel) — aperiodic transition points
    gravel_frac = _jit_abs(seed, 10, 0.60, 0.80)
    n_trans = 2  # one gravel block in the middle
    g_start = _jit_abs(seed, 11, 0.10, 0.18)
    g_end = g_start + gravel_frac
    if g_end > 0.95:
        g_end = 0.95
    pattern = [
        (0.0, g_start, "asphalt"),
        (g_start, g_end, "gravel"),
        (g_end, 1.0, "asphalt"),
    ]
    surf = mixed_surface_segments(total_km, seed, pattern)
    return _finalize(sections, seed, total_km, surf, "rolling", "none",
                     f"gravel_rolling_strade_T{t}")


# gravel_forest_rollercoaster: GV_DUST_IN_THE_WIND / GV_FOREST_TRAIL

def _tpl_gv_dust_in_the_wind(total_km: float, seed: int) -> list[Section]:
    """GV_DUST_IN_THE_WIND — wild rollercoaster with kickers and descents."""
    intro = _jit_abs(seed, 1, 0.4, 0.7)
    remain = total_km - intro
    n = 4 + int(seeded_random(seed, 2) * 2)
    hill_ws = [_jit_abs(seed, 100 + i, 0.25, 0.45) for i in range(n)]
    desc_ws = [_jit_abs(seed, 200 + i, 0.30, 0.55) for i in range(n)]
    roll_ws = _split_poisson(remain - sum(hill_ws) - sum(desc_ws),
                             n + 1, seed + 2301, min_frac=0.25)
    sections = [Section("flat", intro, {"grade": _jit_abs(seed, 3, 0.0, 0.2)})]
    for k in range(n):
        sections.append(Section("rolling", roll_ws[k],
                                {"baseline": _jit_abs(seed, 300 + k, -0.3, 0.5),
                                 "amp": _jit_abs(seed, 400 + k, 3.5, 4.8),
                                 "period_km": _jit_abs(seed, 500 + k, 0.8, 1.2)}))
        sections.append(Section("kicker_up" if seeded_random(seed, 600 + k) < 0.5 else "short_hill",
                                hill_ws[k],
                                {"peak": _jit_abs(seed, 700 + k, 8.0, 11.0)}))
        sections.append(Section("descent", desc_ws[k],
                                {"grade": _jit_abs(seed, 800 + k, -6.0, -4.0)}))
    sections.append(Section("rolling", roll_ws[-1],
                            {"baseline": _jit_abs(seed, 900, 0.0, 0.4),
                             "amp": _jit_abs(seed, 901, 3.0, 4.2),
                             "period_km": _jit_abs(seed, 902, 1.0, 1.5)}))
    return sections


def _tpl_gv_forest_trail(total_km: float, seed: int) -> list[Section]:
    """Pure rolling rollercoaster (aperiodic rolling feature spacing)."""
    return [Section("flat", 0.5, {"grade": _jit_abs(seed, 1, 0.0, 0.2)}),
            Section("rolling", total_km - 0.5,
                    {"baseline": _jit_abs(seed, 2, -0.1, 0.5),
                     "amp": _jit_abs(seed, 3, 4.0, 5.2),
                     "period_km": _jit_abs(seed, 4, 0.9, 1.4)})]


def _tpl_gv_belle_isle_chunky(total_km: float, seed: int) -> list[Section]:
    """Belle Isle style — alternating 1 km up + 1 km down blocks (aperiodic)."""
    remain = total_km - 0.4
    n = max(3, int(remain / _jit_abs(seed, 1, 1.6, 2.4)))
    up_ws = [_jit_abs(seed, 100 + i, 0.6, 1.0) for i in range(n)]
    dn_ws = [_jit_abs(seed, 200 + i, 0.6, 1.0) for i in range(n)]
    used = sum(up_ws) + sum(dn_ws)
    if used > remain * 0.95:
        scale = remain * 0.95 / used
        up_ws = [w * scale for w in up_ws]
        dn_ws = [w * scale for w in dn_ws]
        used = sum(up_ws) + sum(dn_ws)
    pad_ws = _split_poisson(remain - used, n + 1, seed + 2302, min_frac=0.2)
    sections = [Section("flat", 0.4, {"grade": _jit_abs(seed, 2, 0.0, 0.2)})]
    for k in range(n):
        sections.append(Section("flat", pad_ws[k], {"grade": _jit_abs(seed, 300 + k, -0.2, 0.3)}))
        sections.append(Section("short_hill", up_ws[k],
                                {"peak": _jit_abs(seed, 400 + k, 3.5, 5.5)}))
        sections.append(Section("descent", dn_ws[k],
                                {"grade": _jit_abs(seed, 500 + k, -2.5, -1.5)}))
    sections.append(Section("flat", pad_ws[-1], {"grade": _jit_abs(seed, 600, -0.1, 0.2)}))
    return sections


_GV_FOREST_TEMPLATES = [
    _tpl_gv_dust_in_the_wind, _tpl_gv_forest_trail, _tpl_gv_belle_isle_chunky,
]


def gravel_forest_rollercoaster(total_km: float, seed: int) -> ArchetypeOutput:
    """Forest rollercoaster — high variance, 100% gravel. 3 templates."""
    t = _pick_template(seed, len(_GV_FOREST_TEMPLATES))
    sections = _GV_FOREST_TEMPLATES[t](total_km, seed)
    surf = uniform_surface(total_km, "gravel")
    return _finalize(sections, seed, total_km, surf, "rolling", "none",
                     f"gravel_forest_rollercoaster_T{t}")


# gravel_climb_mountain: GV_MOUNTAIN_KING / GV_JUNGLE_CIRCUIT_LITE

def _tpl_gv_mountain_king(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """GV_MOUNTAIN_KING — long sustained gravel climb."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 4.5, 6.5)
    return lead + [
        Section("gradual_climb", core,
                {"avg": avg, "roll_amp": _jit_abs(seed, 2, 1.3, 1.6)})
    ]


def _tpl_gv_jungle_circuit(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Jungle Circuit — gravel climb with plateau and kicker."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 5.0, 6.5)
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in
         enumerate([0.30, 0.06, 0.05, 0.59])]
    s = sum(p); p = [x / s for x in p]
    return lead + [
        Section("sustained_climb", core * p[0], {"avg": avg, "roll_amp": 1.5}),
        Section("kicker_up", core * p[1], {"peak": avg + _jit_abs(seed, 2, 4.0, 6.0)}),
        Section("plateau", core * p[2], {"grade": _jit_abs(seed, 3, 2.0, 3.0)}),
        Section("sustained_climb", core * p[3],
                {"avg": avg + 0.5, "roll_amp": 1.4}),
    ]


def _tpl_gv_mountain_stepped(total_km: float, seed: int, lead: list[Section]) -> list[Section]:
    """Stepped gravel climb — two different intensities."""
    core = total_km - sum(s.length_km for s in lead)
    avg = _jit_abs(seed, 1, 4.5, 6.0)
    lens = _split_poisson(core, 2, seed + 2401, min_frac=0.5)
    return lead + [
        Section("gradual_climb", lens[0],
                {"avg": avg - _jit_abs(seed, 2, 0.3, 0.8), "roll_amp": 1.2}),
        Section("sustained_climb", lens[1],
                {"avg": avg + _jit_abs(seed, 3, 1.0, 2.0), "roll_amp": 1.5}),
    ]


_GV_CLIMB_TEMPLATES = [
    _tpl_gv_mountain_king, _tpl_gv_jungle_circuit, _tpl_gv_mountain_stepped,
]


def gravel_climb_mountain(total_km: float, seed: int) -> ArchetypeOutput:
    """Long gravel climb. 3 templates."""
    t = _pick_template(seed, len(_GV_CLIMB_TEMPLATES))
    lead = _lead_in(seed, probability=0.30)
    sections = _GV_CLIMB_TEMPLATES[t](total_km, seed, lead)
    surf = uniform_surface(total_km, "gravel")
    return _finalize(sections, seed, total_km, surf, "climb", "summit",
                     f"gravel_climb_mountain_T{t}")


# gravel_with_descent: GV_JUNGLE_DESCENT / GV_GRAVEL_GRAN_FONDO

def _tpl_gv_jungle_descent(total_km: float, seed: int) -> list[Section]:
    """Rolling body into long sustained descent."""
    desc_km = _jit_abs(seed, 1, min(5.5, total_km * 0.22), min(6.5, total_km * 0.30))
    body = total_km - desc_km
    return [
        Section("rolling", body,
                {"baseline": _jit_abs(seed, 2, 0.3, 0.7),
                 "amp": _jit_abs(seed, 3, 2.8, 3.4),
                 "period_km": _jit_abs(seed, 4, 1.8, 2.4)}),
        Section("descent", desc_km,
                {"grade": _jit_abs(seed, 5, -6.5, -5.0)}),
    ]


def _tpl_gv_gran_fondo(total_km: float, seed: int) -> list[Section]:
    """Flat + hill + rolling + long descent tail."""
    desc_km = _jit_abs(seed, 1, 4.0, 6.0)
    body = total_km - desc_km
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in enumerate([0.20, 0.10, 0.70])]
    s = sum(p); p = [x / s for x in p]
    return [
        Section("flat", body * p[0], {"grade": _jit_abs(seed, 2, 0.0, 0.3)}),
        Section("short_hill", body * p[1], {"peak": _jit_abs(seed, 3, 6.5, 8.0)}),
        Section("rolling", body * p[2],
                {"baseline": _jit_abs(seed, 4, 0.2, 0.5),
                 "amp": _jit_abs(seed, 5, 2.3, 2.8),
                 "period_km": _jit_abs(seed, 6, 1.6, 2.2)}),
        Section("descent", desc_km, {"grade": _jit_abs(seed, 7, -6.0, -4.5)}),
    ]


def _tpl_gv_mountain_descent(total_km: float, seed: int) -> list[Section]:
    """Climb + plateau + rolling + descent."""
    desc_km = _jit_abs(seed, 1, 4.5, 6.0)
    body = total_km - desc_km
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in enumerate([0.50, 0.15, 0.35])]
    s = sum(p); p = [x / s for x in p]
    return [
        Section("gradual_climb", body * p[0],
                {"avg": _jit_abs(seed, 2, 3.5, 4.5), "roll_amp": 1.2}),
        Section("plateau", body * p[1], {"grade": _jit_abs(seed, 3, 1.5, 2.5)}),
        Section("rolling", body * p[2],
                {"baseline": _jit_abs(seed, 4, 0.2, 0.5),
                 "amp": _jit_abs(seed, 5, 1.8, 2.3),
                 "period_km": _jit_abs(seed, 6, 1.4, 1.8)}),
        Section("descent", desc_km, {"grade": _jit_abs(seed, 7, -6.5, -5.0)}),
    ]


_GV_DESCENT_TEMPLATES = [
    _tpl_gv_jungle_descent, _tpl_gv_gran_fondo, _tpl_gv_mountain_descent,
]


def gravel_with_descent(total_km: float, seed: int) -> ArchetypeOutput:
    """Rolling body + long descent tail. 3 templates."""
    t = _pick_template(seed, len(_GV_DESCENT_TEMPLATES))
    sections = _GV_DESCENT_TEMPLATES[t](total_km, seed)
    surf = uniform_surface(total_km, "gravel")
    return _finalize(sections, seed, total_km, surf, "mixed", "descent",
                     f"gravel_with_descent_T{t}")


# gravel_adventure_long: GV_ROAD_TO_RUINS_FULL / GV_SAND_AND_SEQUOIAS /
#                         GV_FOUR_HORSEMEN_LITE

def _tpl_gv_road_to_ruins_full(total_km: float, seed: int) -> list[Section]:
    """Long gradual climb majority, short descent tail."""
    tail = _jit_abs(seed, 1, total_km * 0.10, total_km * 0.20)
    climb = total_km - tail
    return [
        Section("gradual_climb", climb,
                {"avg": _jit_abs(seed, 2, 0.8, 1.2), "roll_amp": 1.3}),
        Section("descent", tail, {"grade": _jit_abs(seed, 3, -2.5, -1.5)}),
    ]


def _tpl_gv_sand_and_sequoias(total_km: float, seed: int) -> list[Section]:
    """Half desert flat + half forest rollercoaster."""
    lens = _split_poisson(total_km, 2, seed + 2501, min_frac=0.4)
    return [
        Section("flat", lens[0], {"grade": _jit_abs(seed, 1, -0.1, 0.3)}),
        Section("rolling", lens[1],
                {"baseline": _jit_abs(seed, 2, 0.0, 0.3),
                 "amp": _jit_abs(seed, 3, 3.0, 4.0),
                 "period_km": _jit_abs(seed, 4, 0.9, 1.3)}),
    ]


def _tpl_gv_four_horsemen_lite(total_km: float, seed: int) -> list[Section]:
    """Four climbs separated by valleys (epic route shape)."""
    intro = _jit_abs(seed, 1, 2.5, 4.0)
    remain = total_km - intro
    n = 4
    # Each climb: gradual_climb + short_hill + descent + flat
    climb_ws = [_jit_abs(seed, 100 + i, 0.6, 1.2) for i in range(n)]
    desc_ws = [_jit_abs(seed, 200 + i, 0.8, 1.5) for i in range(n)]
    used = sum(climb_ws) + sum(desc_ws)
    slot_pads = _split_poisson(remain - used, n + 1, seed + 2502, min_frac=0.3)
    sections = [Section("flat", intro, {"grade": _jit_abs(seed, 2, 0.0, 0.2)})]
    for k in range(n):
        sections.append(Section("rolling", slot_pads[k],
                                {"baseline": _jit_abs(seed, 300 + k, 0.3, 0.6),
                                 "amp": _jit_abs(seed, 400 + k, 1.8, 2.5),
                                 "period_km": _jit_abs(seed, 500 + k, 1.8, 2.5)}))
        peak = _jit_abs(seed, 600 + k, 7.0 + k * 0.3, 9.0 + k * 0.3)
        kind = "short_hill" if peak < 8.5 else "kicker_up"
        sections.append(Section(kind, climb_ws[k], {"peak": peak}))
        sections.append(Section("descent", desc_ws[k],
                                {"grade": _jit_abs(seed, 700 + k, -4.0, -2.8)}))
    sections.append(Section("rolling", slot_pads[-1],
                            {"baseline": _jit_abs(seed, 800, 0.0, 0.3),
                             "amp": _jit_abs(seed, 801, 1.5, 2.2),
                             "period_km": _jit_abs(seed, 802, 1.8, 2.5)}))
    return sections


_GV_ADVENTURE_TEMPLATES = [
    _tpl_gv_road_to_ruins_full, _tpl_gv_sand_and_sequoias,
    _tpl_gv_four_horsemen_lite,
]


def gravel_adventure_long(total_km: float, seed: int) -> ArchetypeOutput:
    """Long gravel adventure 60-120 km. 3 templates."""
    t = _pick_template(seed, len(_GV_ADVENTURE_TEMPLATES))
    sections = _GV_ADVENTURE_TEMPLATES[t](total_km, seed)
    gravel_frac = _jit_abs(seed, 50, 0.55, 0.70)
    pattern = [
        (0.0, 0.15, "asphalt"),
        (0.15, 0.15 + gravel_frac, "gravel"),
        (0.15 + gravel_frac, 1.0, "asphalt"),
    ]
    surf = mixed_surface_segments(total_km, seed, pattern)
    return _finalize(sections, seed, total_km, surf, "mixed", "none",
                     f"gravel_adventure_long_T{t}")


# ── MIXED FAMILY (2) ─────────────────────────────────────────────────────────

# mixed_asphalt_gravel_sandwich: MX_SAND_SEQUOIAS_MIX / MX_FOUR_HORSEMEN_LITE

def _tpl_mx_sand_sequoias(total_km: float, seed: int) -> list[Section]:
    """Flat bookends + rolling gravel middle."""
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in enumerate([0.22, 0.56, 0.22])]
    s = sum(p); p = [x / s for x in p]
    return [
        Section("flat", total_km * p[0], {"grade": _jit_abs(seed, 1, 0.0, 0.3)}),
        Section("rolling", total_km * p[1],
                {"baseline": _jit_abs(seed, 2, 0.2, 0.5),
                 "amp": _jit_abs(seed, 3, 2.3, 2.8),
                 "period_km": _jit_abs(seed, 4, 1.8, 2.4)}),
        Section("flat", total_km * p[2], {"grade": _jit_abs(seed, 5, -0.1, 0.2)}),
    ]


def _tpl_mx_four_horsemen_lite(total_km: float, seed: int) -> list[Section]:
    """Two climbs + rolling + flat, all mixed surface."""
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in
         enumerate([0.20, 0.25, 0.20, 0.20, 0.15])]
    s = sum(p); p = [x / s for x in p]
    return [
        Section("false_flat_up", total_km * p[0],
                {"grade": _jit_abs(seed, 1, 1.3, 2.0)}),
        Section("rolling", total_km * p[1],
                {"baseline": _jit_abs(seed, 2, 0.2, 0.4),
                 "amp": _jit_abs(seed, 3, 2.3, 2.8),
                 "period_km": _jit_abs(seed, 4, 1.9, 2.4)}),
        Section("short_hill", total_km * p[2],
                {"peak": _jit_abs(seed, 5, 6.0, 7.5)}),
        Section("rolling", total_km * p[3],
                {"baseline": _jit_abs(seed, 6, 0.1, 0.3),
                 "amp": _jit_abs(seed, 7, 1.8, 2.3),
                 "period_km": _jit_abs(seed, 8, 1.8, 2.4)}),
        Section("flat", total_km * p[4], {"grade": _jit_abs(seed, 9, -0.2, 0.2)}),
    ]


def _tpl_mx_climb_then_rolling(total_km: float, seed: int) -> list[Section]:
    """Flat + gradual climb + descent + rolling."""
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in
         enumerate([0.25, 0.25, 0.20, 0.30])]
    s = sum(p); p = [x / s for x in p]
    return [
        Section("flat", total_km * p[0], {"grade": _jit_abs(seed, 1, 0.0, 0.3)}),
        Section("gradual_climb", total_km * p[1],
                {"avg": _jit_abs(seed, 2, 3.5, 4.5), "roll_amp": 1.2}),
        Section("descent", total_km * p[2],
                {"grade": _jit_abs(seed, 3, -3.5, -2.5)}),
        Section("rolling", total_km * p[3],
                {"baseline": _jit_abs(seed, 4, 0.1, 0.3),
                 "amp": _jit_abs(seed, 5, 1.8, 2.2),
                 "period_km": _jit_abs(seed, 6, 1.8, 2.2)}),
    ]


_MX_SANDWICH_TEMPLATES = [
    _tpl_mx_sand_sequoias, _tpl_mx_four_horsemen_lite, _tpl_mx_climb_then_rolling,
]


def mixed_asphalt_gravel_sandwich(total_km: float, seed: int) -> ArchetypeOutput:
    """Asphalt-gravel-asphalt sandwich. 3 templates."""
    t = _pick_template(seed, len(_MX_SANDWICH_TEMPLATES))
    sections = _MX_SANDWICH_TEMPLATES[t](total_km, seed)
    surf = mixed_surface_segments(total_km, seed, [
        (0.0, 0.30, "asphalt"),
        (0.30, 0.70, "gravel"),
        (0.70, 1.0, "asphalt"),
    ])
    return _finalize(sections, seed, total_km, surf, "rolling", "none",
                     f"mixed_asphalt_gravel_sandwich_T{t}")


# mixed_gravel_finish: MX_MEGA_PRETZEL_FINISH / MX_RURAL_TO_GRAVEL /
#                       MX_ROLLING_TO_CLIMB

def _tpl_mx_mega_pretzel_finish(total_km: float, seed: int) -> list[Section]:
    gravel_km = _jit_abs(seed, 1, min(4.5, total_km * 0.20),
                         min(5.5, total_km * 0.28))
    body = total_km - gravel_km
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in enumerate([0.30, 0.70])]
    s = sum(p); p = [x / s for x in p]
    avg_final = _jit_abs(seed, 2, 5.5, 7.5)
    return [
        Section("flat", body * p[0], {"grade": _jit_abs(seed, 3, 0.0, 0.3)}),
        Section("rolling", body * p[1],
                {"baseline": _jit_abs(seed, 4, 0.2, 0.5),
                 "amp": _jit_abs(seed, 5, 2.0, 2.5),
                 "period_km": _jit_abs(seed, 6, 1.8, 2.2)}),
        Section("sustained_climb", gravel_km,
                {"avg": avg_final, "roll_amp": 1.3}),
    ]


def _tpl_mx_rural_to_gravel(total_km: float, seed: int) -> list[Section]:
    """Rolling rural + gravel climb."""
    gravel_km = _jit_abs(seed, 1, 3.0, 5.0)
    body = total_km - gravel_km
    return [
        Section("rolling", body,
                {"baseline": _jit_abs(seed, 2, 0.2, 0.4),
                 "amp": _jit_abs(seed, 3, 2.3, 2.8),
                 "period_km": _jit_abs(seed, 4, 1.8, 2.4)}),
        Section("gradual_climb", gravel_km,
                {"avg": _jit_abs(seed, 5, 5.0, 7.0), "roll_amp": 1.2}),
    ]


def _tpl_mx_rolling_to_climb(total_km: float, seed: int) -> list[Section]:
    """Flat + short hill + rolling + gravel climb + kicker."""
    gravel_km = _jit_abs(seed, 1, 3.5, 5.0)
    body = total_km - gravel_km
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in enumerate([0.45, 0.15, 0.40])]
    s = sum(p); p = [x / s for x in p]
    kicker_avg = _jit_abs(seed, 2, 5.5, 7.0)
    return [
        Section("flat", body * p[0], {"grade": _jit_abs(seed, 3, 0.0, 0.3)}),
        Section("short_hill", body * p[1], {"peak": _jit_abs(seed, 4, 5.5, 7.0)}),
        Section("rolling", body * p[2],
                {"baseline": _jit_abs(seed, 5, 0.2, 0.4),
                 "amp": _jit_abs(seed, 6, 1.8, 2.3),
                 "period_km": _jit_abs(seed, 7, 1.7, 2.2)}),
        Section("sustained_climb", gravel_km * 0.7,
                {"avg": kicker_avg, "roll_amp": 1.3}),
        Section("kicker_up", gravel_km * 0.3,
                {"peak": kicker_avg + _jit_abs(seed, 8, 4.0, 5.5)}),
    ]


_MX_GRAVEL_FINISH_TEMPLATES = [
    _tpl_mx_mega_pretzel_finish, _tpl_mx_rural_to_gravel, _tpl_mx_rolling_to_climb,
]


def mixed_gravel_finish(total_km: float, seed: int) -> ArchetypeOutput:
    """Rolling asphalt body + gravel climb finish. 3 templates."""
    t = _pick_template(seed, len(_MX_GRAVEL_FINISH_TEMPLATES))
    sections = _MX_GRAVEL_FINISH_TEMPLATES[t](total_km, seed)
    # Last section(s) define the gravel finish — total climb portion
    gravel_len = sum(s.length_km for s in sections
                     if s.kind in {"gradual_climb", "sustained_climb",
                                   "kicker_up", "steep_wall"})
    # But only consider the last stretch — we place gravel at tail
    body_len = total_km - gravel_len
    # For template t=2 (rolling_to_climb), sustain+kicker are both gravel; use sum.
    if body_len <= 0:
        body_len = total_km * 0.6
    surf = mixed_surface_segments(total_km, seed, [
        (0.0, body_len / total_km, "asphalt"),
        (body_len / total_km, 1.0, "gravel"),
    ])
    return _finalize(sections, seed, total_km, surf, "mixed", "summit",
                     f"mixed_gravel_finish_T{t}")


# ── LAP FAMILY (5) ───────────────────────────────────────────────────────────

def _pick_lap_params(total_km: float, seed: int,
                     base_range: tuple[float, float] = (2.0, 8.0),
                     lap_candidates: Optional[list[int]] = None
                     ) -> tuple[float, int]:
    if lap_candidates is None:
        lap_candidates = [2, 3, 5, 8, 10]
    best = None
    best_err = 1e9
    for lc in lap_candidates:
        base = total_km / lc
        if base_range[0] <= base <= base_range[1]:
            err = abs(base - (base_range[0] + base_range[1]) / 2)
            if err < best_err:
                best_err = err
                best = (round(base, 3), lc)
    if best is None:
        lc = max(2, min(10, int(round(total_km / sum(base_range) * 2))))
        base = total_km / lc
        best = (round(base, 3), lc)
    return best


def _varied_laps(templates, base_km_target: float, lap_count: int,
                 outer_seed: int, surface: str = "asphalt",
                 enforce_net_zero: bool = True):
    """Concatenate `lap_count` laps where EACH lap picks a different template
    and a slightly different base_km. Addresses the "same hill N times" look
    by rotating through the template palette with independent seeds.

    Returns `(segs, grades, surface_segments)` — same ABI as `_repeat_lap`.

    When `enforce_net_zero=True` each lap's grade is zero-meaned so the
    route returns to start-elevation per lap (required for crit/kicker
    circuits; disable for one-way gradual climbs where the terrain keeps
    gaining).
    """
    all_segs: list[float] = []
    all_grades: list[float] = []
    surf_records: list[dict] = []
    cumulative_km = 0.0
    n_templates = len(templates)

    # Shuffle a template order up front so consecutive laps are dissimilar.
    # A simple "rotate + offset" guarantees every template appears before any
    # repeats (for lap_count ≤ n_templates) and spread repeats maximally for
    # longer routes.
    start_offset = int(seeded_random(outer_seed, 33) * n_templates)

    for lap in range(lap_count):
        tmpl_idx = (lap + start_offset) % n_templates
        # Fresh seed per lap — params, jitter, noise all differ.
        lap_seed = _hash32(outer_seed, lap * 9931 + 444) & 0xFFFFFFFF
        # Jitter base_km per lap (±15%) so lap lengths vary too.
        jittered_km = max(1.5, base_km_target * (0.85 + seeded_random(lap_seed, 1) * 0.30))
        sections = templates[tmpl_idx](jittered_km, lap_seed)
        lap_segs, lap_grades = build_route_from_sections(sections, lap_seed)

        d_total = sum(lap_segs)
        if enforce_net_zero and d_total:
            wm = sum(g * d for g, d in zip(lap_grades, lap_segs)) / d_total
            lap_grades = [g - wm for g in lap_grades]

        all_segs.extend(lap_segs)
        all_grades.extend(lap_grades)
        surf_records.append({
            "start_km": round(cumulative_km, 4),
            "end_km": round(cumulative_km + d_total, 4),
            "surface": surface,
        })
        cumulative_km += d_total

    # Merge consecutive same-surface entries
    merged = []
    for s in surf_records:
        if merged and merged[-1]["surface"] == s["surface"]:
            merged[-1]["end_km"] = s["end_km"]
        else:
            merged.append(dict(s))
    return all_segs, all_grades, merged


def _repeat_lap(base_segs: list[float], base_grades: list[float],
                base_surface: list[dict], lap_count: int,
                base_loop_km: float, seed: int = 0,
                noise_amp: float = 0.6):
    """Replicate base loop N times, adding independent fBm noise per lap.

    The per-lap fBm jitter breaks the exact periodicity that drives
    max_autocorr ≈ 0.99 for lap archetypes. Each lap gets a different noise
    seed so deltas between consecutive laps are aperiodic. The jitter is
    zero-meaned within each lap so net-zero base loops stay net-zero.
    """
    segs: list[float] = []
    grades: list[float] = []
    n_base = len(base_grades)
    for lap in range(lap_count):
        segs.extend(base_segs)
        lap_seed = _hash32(seed, lap * 9931 + 777) & 0xFFFFFFFF
        # Pre-compute the noise series for this lap, then zero-mean it so
        # the lap's net climb is unchanged.
        lap_noise = [_fbm_1d(i * 0.7, lap_seed, octaves=3, persistence=0.5) * noise_amp
                     for i in range(n_base)]
        noise_mean = sum(lap_noise) / max(1, n_base)
        lap_noise = [v - noise_mean for v in lap_noise]
        for g, jitter in zip(base_grades, lap_noise):
            grades.append(g + jitter)
    surf: list[dict] = []
    for lap in range(lap_count):
        offset = lap * base_loop_km
        for seg in base_surface:
            surf.append({
                "start_km": round(seg["start_km"] + offset, 4),
                "end_km": round(seg["end_km"] + offset, 4),
                "surface": seg["surface"],
            })
    merged = []
    for s in surf:
        if (merged and merged[-1]["surface"] == s["surface"]
                and abs(merged[-1]["end_km"] - s["start_km"]) < 0.001):
            merged[-1]["end_km"] = s["end_km"]
        else:
            merged.append(dict(s))
    return segs, grades, merged


# ── lap_flat_tt: LP_CRIT_CITY_FLAT / LP_VOLCANO_LAP ──

def _tpl_lpf_crit_city(base_km: float, seed: int) -> list[Section]:
    return [Section("flat", base_km, {"grade": _jit_abs(seed, 1, -0.1, 0.2)})]


def _tpl_lpf_volcano(base_km: float, seed: int) -> list[Section]:
    lens = _split_poisson(base_km, 3, seed + 2601, min_frac=0.5)
    return [
        Section("flat", lens[0], {"grade": _jit_abs(seed, 1, -0.1, 0.2)}),
        Section("false_flat_up", lens[1], {"grade": _jit_abs(seed, 2, 0.5, 0.9)}),
        Section("false_flat_down", lens[2], {"grade": _jit_abs(seed, 3, -0.5, -0.2)}),
    ]


def _tpl_lpf_gentle_drift(base_km: float, seed: int) -> list[Section]:
    lens = _split_poisson(base_km, 2, seed + 2602, min_frac=0.6)
    return [
        Section("flat", lens[0], {"grade": _jit_abs(seed, 1, -0.1, 0.2)}),
        Section("rolling", lens[1],
                {"baseline": _jit_abs(seed, 2, 0.0, 0.3),
                 "amp": _jit_abs(seed, 3, 0.5, 0.9),
                 "period_km": _jit_abs(seed, 4, 1.2, 1.8)}),
    ]


_LAP_FLAT_TT_TEMPLATES = [
    _tpl_lpf_crit_city, _tpl_lpf_volcano, _tpl_lpf_gentle_drift,
]


def lap_flat_tt(total_km: float, seed: int) -> ArchetypeOutput:
    """Flat TT laps. Each lap picks a DIFFERENT template so you get
    Volcano-style / Crit-City-style / Gentle-Drift laps mixed together
    rather than the same pattern tiled N times."""
    base_km, lap_count = _pick_lap_params(total_km, seed, base_range=(3.0, 5.0))
    segs, grades, surf = _varied_laps(
        _LAP_FLAT_TT_TEMPLATES, base_km, lap_count, seed,
        surface="asphalt", enforce_net_zero=True,
    )
    return ArchetypeOutput(
        segs=segs, grades=grades, surface_segments=surf,
        terrain="flat", finish_type="sprint_flat",
        climb_count=0, primary_climb=None,
        template_id="lap_flat_tt_varied",
    )


# ── lap_rolling: LP_HILLY_LAP / LP_SHORT_ROLLING_LAP ──

def _tpl_lpr_hilly(base_km: float, seed: int) -> list[Section]:
    """Hill + descent + flat."""
    p = [_jit(seed, 100 + i, b, 0.15) for i, b in enumerate([0.30, 0.30, 0.40])]
    s = sum(p); p = [x / s for x in p]
    return [
        Section("short_hill", base_km * p[0], {"peak": _jit_abs(seed, 1, 4.5, 5.5)}),
        Section("descent", base_km * p[1], {"grade": _jit_abs(seed, 2, -4.5, -3.5)}),
        Section("flat", base_km * p[2], {"grade": _jit_abs(seed, 3, -0.2, 0.2)}),
    ]


def _tpl_lpr_short_rolling(base_km: float, seed: int) -> list[Section]:
    """Rolling lap with asymmetric feature spacing."""
    return [Section("rolling", base_km,
                    {"baseline": _jit_abs(seed, 1, -0.1, 0.3),
                     "amp": _jit_abs(seed, 2, 2.5, 3.2),
                     "period_km": _jit_abs(seed, 3, 1.3, 1.9)})]


def _tpl_lpr_updown(base_km: float, seed: int) -> list[Section]:
    """False-flat up, false-flat down, rolling."""
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in enumerate([0.30, 0.30, 0.40])]
    s = sum(p); p = [x / s for x in p]
    return [
        Section("false_flat_up", base_km * p[0], {"grade": _jit_abs(seed, 1, 1.5, 2.5)}),
        Section("false_flat_down", base_km * p[1], {"grade": _jit_abs(seed, 2, -2.5, -1.5)}),
        Section("rolling", base_km * p[2],
                {"baseline": _jit_abs(seed, 3, -0.1, 0.3),
                 "amp": _jit_abs(seed, 4, 1.8, 2.2),
                 "period_km": _jit_abs(seed, 5, 1.0, 1.4)}),
    ]


def _tpl_lpr_mostly_flat(base_km: float, seed: int) -> list[Section]:
    """Plain gentle lap — two flat stretches + one low roller. Adds a
    'breather' to the rolling series."""
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in enumerate([0.45, 0.20, 0.35])]
    s = sum(p); p = [x / s for x in p]
    return [
        Section("flat", base_km * p[0], {"grade": _jit_abs(seed, 1, -0.2, 0.2)}),
        Section("rolling", base_km * p[1],
                {"baseline": _jit_abs(seed, 2, 0.0, 0.3),
                 "amp": _jit_abs(seed, 3, 0.8, 1.3),
                 "period_km": _jit_abs(seed, 4, 1.0, 1.5)}),
        Section("flat", base_km * p[2], {"grade": _jit_abs(seed, 5, -0.2, 0.2)}),
    ]


_LAP_ROLLING_TEMPLATES = [
    _tpl_lpr_hilly, _tpl_lpr_short_rolling, _tpl_lpr_updown,
    _tpl_lpr_mostly_flat,
]


def lap_rolling(total_km: float, seed: int) -> ArchetypeOutput:
    """Rolling laps — each lap picks a different template (varied hill
    heights, some mostly-flat laps in between) instead of tiling one
    pattern N times."""
    base_km, lap_count = _pick_lap_params(total_km, seed, base_range=(3.0, 6.0))
    segs, grades, surf = _varied_laps(
        _LAP_ROLLING_TEMPLATES, base_km, lap_count, seed,
        surface="asphalt", enforce_net_zero=True,
    )
    return ArchetypeOutput(
        segs=segs, grades=grades, surface_segments=surf,
        terrain="rolling", finish_type="none",
        climb_count=0, primary_climb=None,
        template_id="lap_rolling_varied",
    )


# ── lap_climb: LP_EPIC_KOM_REPEAT_LITE ──

def _tpl_lpc_climb_descent(base_km: float, seed: int) -> list[Section]:
    """Simple: climb + descent, net-zero."""
    climb_avg = _jit_abs(seed, 1, 5.5, 7.5)
    return [
        Section("gradual_climb", base_km * 0.5,
                {"avg": climb_avg, "roll_amp": 1.0}),
        Section("descent", base_km * 0.5, {"grade": -climb_avg}),
    ]


def _tpl_lpc_sustained_plateau_descent(base_km: float, seed: int) -> list[Section]:
    climb_avg = _jit_abs(seed, 1, 6.0, 7.5)
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in enumerate([0.45, 0.05, 0.50])]
    s = sum(p); p = [x / s for x in p]
    desc_grade = -(climb_avg * p[0] + 2.5 * p[1]) / p[2]
    return [
        Section("sustained_climb", base_km * p[0],
                {"avg": climb_avg, "roll_amp": 1.3}),
        Section("plateau", base_km * p[1],
                {"grade": _jit_abs(seed, 2, 2.0, 3.0)}),
        Section("descent", base_km * p[2], {"grade": desc_grade}),
    ]


def _tpl_lpc_with_leadup(base_km: float, seed: int) -> list[Section]:
    climb_avg = _jit_abs(seed, 1, 6.5, 8.0)
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in enumerate([0.10, 0.40, 0.50])]
    s = sum(p); p = [x / s for x in p]
    desc_grade = -(climb_avg * p[1] + 2.0 * p[0]) / p[2]
    return [
        Section("false_flat_up", base_km * p[0],
                {"grade": _jit_abs(seed, 2, 1.5, 2.5)}),
        Section("sustained_climb", base_km * p[1],
                {"avg": climb_avg, "roll_amp": 1.3}),
        Section("descent", base_km * p[2], {"grade": desc_grade}),
    ]


_LAP_CLIMB_TEMPLATES = [
    _tpl_lpc_climb_descent, _tpl_lpc_sustained_plateau_descent, _tpl_lpc_with_leadup,
]


def lap_climb(total_km: float, seed: int) -> ArchetypeOutput:
    """Climb-descent laps. Each lap picks a different climb template
    (different climb height, different descent profile, some with lead-up)
    rather than tiling one loop N times."""
    base_km, lap_count = _pick_lap_params(total_km, seed, base_range=(3.0, 6.0))
    segs, grades, surf = _varied_laps(
        _LAP_CLIMB_TEMPLATES, base_km, lap_count, seed,
        surface="asphalt", enforce_net_zero=True,
    )
    # Compute primary_climb from the observed max grade across all laps
    if grades:
        max_idx = max(range(len(grades)), key=lambda i: grades[i])
        max_g = grades[max_idx]
    else:
        max_g = 0
    primary = {
        "start_km": 0.0,
        "length_km": round(base_km * 0.5, 2),
        "avg_grade": round(max(4.0, max_g * 0.6), 2),
        "max_grade": round(max_g, 2),
    }
    return ArchetypeOutput(
        segs=segs, grades=grades, surface_segments=surf,
        terrain="climb", finish_type="none",
        climb_count=lap_count, primary_climb=primary,
        template_id="lap_climb_varied",
    )


# ── lap_punchy_kicker: LP_KICKER_LAP ──

def _tpl_lpk_flat_kicker(base_km: float, seed: int) -> list[Section]:
    wall_len = min(0.5, base_km * _jit(seed, 1, 0.15, 0.15))
    descent_len = _jit_abs(seed, 2, 0.4, 0.6)
    flat_len = base_km - wall_len - descent_len
    return [
        Section("flat", flat_len, {"grade": _jit_abs(seed, 3, 0.0, 0.3)}),
        Section("kicker_up", wall_len, {"peak": _jit_abs(seed, 4, 9.0, 11.5)}),
        Section("descent", descent_len, {"grade": _jit_abs(seed, 5, -4.5, -3.5)}),
    ]


def _tpl_lpk_rolling_approach(base_km: float, seed: int) -> list[Section]:
    wall_len = min(0.5, base_km * _jit(seed, 1, 0.15, 0.15))
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in enumerate([0.48, 0.30, 0.22])]
    s = sum(p); p = [x / s for x in p]
    roll_len = (base_km - wall_len) * p[0]
    flat_len = (base_km - wall_len) * p[1]
    desc_len = (base_km - wall_len) * p[2]
    return [
        Section("rolling", roll_len,
                {"baseline": _jit_abs(seed, 2, 0.1, 0.4),
                 "amp": _jit_abs(seed, 3, 1.3, 1.8),
                 "period_km": _jit_abs(seed, 4, 0.9, 1.2)}),
        Section("flat", flat_len, {"grade": _jit_abs(seed, 5, 0.0, 0.2)}),
        Section("kicker_up", wall_len, {"peak": _jit_abs(seed, 6, 9.5, 11.0)}),
        Section("descent", desc_len, {"grade": _jit_abs(seed, 7, -4.5, -3.5)}),
    ]


def _tpl_lpk_hill_then_descent(base_km: float, seed: int) -> list[Section]:
    wall_len = min(0.5, base_km * _jit(seed, 1, 0.15, 0.15))
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in enumerate([0.40, 0.30, 0.30])]
    s = sum(p); p = [x / s for x in p]
    flat1 = (base_km - wall_len) * p[0]
    descent = (base_km - wall_len) * p[1]
    flat2 = (base_km - wall_len) * p[2]
    return [
        Section("flat", flat1, {"grade": _jit_abs(seed, 2, 0.0, 0.2)}),
        Section("kicker_up", wall_len, {"peak": _jit_abs(seed, 3, 10.5, 12.0)}),
        Section("descent", descent, {"grade": _jit_abs(seed, 4, -3.5, -2.5)}),
        Section("flat", flat2, {"grade": _jit_abs(seed, 5, -0.1, 0.2)}),
    ]


def _tpl_lpk_just_flat(base_km: float, seed: int) -> list[Section]:
    """Plain flat lap — no kicker at all. Breaks the "same hill N times"
    pattern when used alongside other templates."""
    return [
        Section("flat", base_km, {"grade": _jit_abs(seed, 1, -0.2, 0.2)}),
    ]


def _tpl_lpk_small_hill(base_km: float, seed: int) -> list[Section]:
    """Small hill (not a wall) — gives the series a gentler variant."""
    hill_len = min(0.8, base_km * _jit(seed, 1, 0.22, 0.10))
    p = [_jit(seed, 100 + i, b, 0.12) for i, b in enumerate([0.45, 0.30, 0.25])]
    s = sum(p); p = [x / s for x in p]
    flat1 = (base_km - hill_len) * p[0]
    descent = (base_km - hill_len) * p[1]
    flat2 = (base_km - hill_len) * p[2]
    return [
        Section("flat", flat1, {"grade": _jit_abs(seed, 2, 0.0, 0.2)}),
        Section("short_hill", hill_len, {"peak": _jit_abs(seed, 3, 4.0, 6.5)}),
        Section("descent", descent, {"grade": _jit_abs(seed, 4, -2.5, -1.8)}),
        Section("flat", flat2, {"grade": _jit_abs(seed, 5, -0.2, 0.2)}),
    ]


_LAP_PUNCHY_TEMPLATES = [
    _tpl_lpk_flat_kicker,       # the wall variant
    _tpl_lpk_rolling_approach,  # rolling lead-in + wall
    _tpl_lpk_hill_then_descent, # wall with descent after
    _tpl_lpk_just_flat,         # plain flat — for variety between kickers
    _tpl_lpk_small_hill,        # smaller hill — mid-difficulty variant
]


def lap_punchy_kicker(total_km: float, seed: int) -> ArchetypeOutput:
    """Kicker laps — but each lap is DIFFERENT. Rotation picks from 5
    templates (flat-kicker / rolling-approach / hill-descent / just-flat /
    small-hill) so a 5-lap route shows flat→small-hill→rolling-kicker→
    flat→wall, not the same kicker 5 times. Fixes the "High Ridgeline x8"
    repetition bug."""
    base_km, lap_count = _pick_lap_params(total_km, seed, base_range=(3.0, 6.0))
    segs, grades, surf = _varied_laps(
        _LAP_PUNCHY_TEMPLATES, base_km, lap_count, seed,
        surface="asphalt", enforce_net_zero=True,
    )
    max_g = max(grades) if grades else 0.0
    primary = {
        "start_km": 0.0,
        "length_km": round(min(0.5, base_km * 0.15), 2),
        "avg_grade": round(max_g, 2),
        "max_grade": round(max_g, 2),
    }
    return ArchetypeOutput(
        segs=segs, grades=grades, surface_segments=surf,
        terrain="rolling", finish_type="wall",
        climb_count=lap_count, primary_climb=primary,
        template_id="lap_punchy_kicker_varied",
    )


# ── lap_criterium: LP_CRIT_CITY_TURNS ──

def _tpl_lpcr_flat(base_km: float, seed: int) -> list[Section]:
    return [Section("flat", base_km, {"grade": _jit_abs(seed, 1, -0.1, 0.2)})]


def _tpl_lpcr_slight_descent(base_km: float, seed: int) -> list[Section]:
    lens = _split_poisson(base_km, 2, seed + 2701, min_frac=0.5)
    return [
        Section("flat", lens[0], {"grade": _jit_abs(seed, 1, 0.1, 0.3)}),
        Section("false_flat_down", lens[1], {"grade": _jit_abs(seed, 2, -0.4, -0.1)}),
    ]


def _tpl_lpcr_micro_rolling(base_km: float, seed: int) -> list[Section]:
    return [Section("rolling", base_km,
                    {"baseline": _jit_abs(seed, 1, -0.1, 0.05),
                     "amp": _jit_abs(seed, 2, 0.25, 0.45),
                     "period_km": _jit_abs(seed, 3, 0.4, 0.7)})]


_LAP_CRIT_TEMPLATES = [
    _tpl_lpcr_flat, _tpl_lpcr_slight_descent, _tpl_lpcr_micro_rolling,
]


def lap_criterium(total_km: float, seed: int) -> ArchetypeOutput:
    """Pure criterium 1-2 km loop. 3 templates."""
    base_km, lap_count = _pick_lap_params(total_km, seed, base_range=(1.0, 2.0),
                                          lap_candidates=[5, 8, 10, 12, 15])
    t = _pick_template(seed, len(_LAP_CRIT_TEMPLATES))
    sections = _LAP_CRIT_TEMPLATES[t](base_km, seed)
    base_segs, base_grades = build_route_from_sections(sections, seed)
    base_surf = uniform_surface(sum(base_segs), "asphalt")
    segs, grades, surf = _repeat_lap(base_segs, base_grades, base_surf,
                                     lap_count, sum(base_segs), seed=seed,
                                     noise_amp=0.20)
    return ArchetypeOutput(
        segs=segs, grades=grades, surface_segments=surf,
        terrain="flat", finish_type="sprint_flat",
        climb_count=0, primary_climb=None,
        template_id=f"lap_criterium_T{t}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Legacy primitive helpers (kept for test_route_archetypes.py back-compat)
# ─────────────────────────────────────────────────────────────────────────────

def _warmup_ramp(t: float, intro_pct: float, intro_grade: float, main_grade: float) -> float:
    if t < intro_pct:
        tt = t / intro_pct
        return intro_grade + (main_grade - intro_grade) * _smoothstep(tt * 0.5)
    return main_grade


def _false_plateau(grades: list[float], at_pct: float, width: float, dip_to: float
                   ) -> list[float]:
    """Blend a bell-shaped plateau/dip into grades around ``at_pct``.

    Uses a Gaussian bell for the blend weight (not a half-sine), which
    keeps the primitive file free of sinusoidal operators while producing
    a smooth symmetric plateau.
    """
    n = len(grades)
    if n == 0:
        return grades
    start = max(0, int((at_pct - width / 2) * n))
    end = min(n, int((at_pct + width / 2) * n))
    out = list(grades)
    span = max(1, end - start)
    for i in range(start, end):
        t = (i - start) / span
        # Gaussian bell centred on 0.5 of the plateau, 0 at edges, 1 at centre
        d = (t - 0.5) / 0.22
        bell = math.exp(-0.5 * d * d)
        out[i] = out[i] + (dip_to - out[i]) * bell
    return out


def _spike(grades: list[float], at_pct: float, width: float, amplitude: float
           ) -> list[float]:
    n = len(grades)
    if n == 0:
        return grades
    out = list(grades)
    center = at_pct * n
    sigma = max(1.0, width * n / 2.355)
    for i in range(n):
        d = (i - center) / sigma
        out[i] += amplitude * math.exp(-0.5 * d * d)
    return out


def _summit_sprint(grades: list[float], final_pct: float, amplitude: float
                   ) -> list[float]:
    n = len(grades)
    if n == 0:
        return grades
    out = list(grades)
    start = int((1.0 - final_pct) * n)
    for i in range(start, n):
        t = (i - start) / max(1, (n - start))
        out[i] += amplitude * _smoothstep(t)
    return out


def _two_stepper(grades: list[float], split: float, grade_a: float,
                 grade_b: float, dip: float) -> list[float]:
    n = len(grades)
    if n == 0:
        return grades
    out = list(grades)
    dip_width = max(1, int(0.10 * n))
    split_idx = int(split * n)
    dip_start = split_idx
    dip_end = min(n, split_idx + dip_width)
    for i in range(n):
        if i < dip_start:
            out[i] = grade_a
        elif i < dip_end:
            t = (i - dip_start) / max(1, dip_width)
            if t < 0.5:
                out[i] = grade_a + (dip - grade_a) * (t / 0.5)
            else:
                out[i] = dip + (grade_b - dip) * ((t - 0.5) / 0.5)
        else:
            out[i] = grade_b
    return out


def _oscillation(grades: list[float], period_km: float, amplitude: float,
                 seg_size_km: float = SEG_KM) -> list[float]:
    """Add aperiodic fBm oscillation with characteristic scale ``period_km``.

    Previously used ``math.sin`` — replaced with fBm tuned so the dominant
    feature scale roughly matches ``period_km`` while being intrinsically
    aperiodic (no autocorrelation peaks).
    """
    n = len(grades)
    if n == 0:
        return grades
    out = list(grades)
    # Map period_km to fBm frequency: one "feature" per period_km.
    # fBm x-argument is in "fBm units"; 1 unit ≈ 1 lowest-octave feature.
    freq = 1.0 / max(0.05, period_km) * seg_size_km
    for i in range(n):
        out[i] += amplitude * _fbm_1d(i * freq, hash(("_osc", period_km)) & 0xFFFF,
                                      octaves=3, persistence=0.55)
    return out


def _gaussian_hump(t: float, center: float, sigma: float, amplitude: float) -> float:
    d = (t - center) / max(1e-6, sigma)
    return amplitude * math.exp(-0.5 * d * d)


def _stepped_trend(t: float, control_pts: list[tuple[float, float]]) -> float:
    if not control_pts:
        return 0.0
    pts = sorted(control_pts)
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for (a, av), (b, bv) in zip(pts, pts[1:]):
        if a <= t <= b:
            frac = (t - a) / max(1e-9, b - a)
            return av + (bv - av) * frac
    return pts[-1][1]


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────

ARCHETYPE_REGISTRY: dict[str, ArchetypeSpec] = {
    # Flat (4)
    "flat_tt": ArchetypeSpec(
        "flat_tt", flat_tt, 10.0, 40.0,
        smoothing_max_change=1.5, max_grade_cap=3.0, min_grade_floor=-3.0,
        family="flat", short_description="Pure flat TT, avg near 0%"),
    "flat_with_sprint": ArchetypeSpec(
        "flat_with_sprint", flat_with_sprint, 10.0, 30.0,
        smoothing_max_change=2.5, max_grade_cap=5.0, min_grade_floor=-3.0,
        family="flat", short_description="Flat with drag to the line"),
    "flat_with_hill_end": ArchetypeSpec(
        "flat_with_hill_end", flat_with_hill_end, 12.0, 30.0,
        smoothing_max_change=3.0, max_grade_cap=9.0, min_grade_floor=-4.0,
        family="flat", short_description="Flat body, short climb to finish"),
    "flat_descending_tt": ArchetypeSpec(
        "flat_descending_tt", flat_descending_tt, 10.0, 30.0,
        smoothing_max_change=1.5, max_grade_cap=3.0, min_grade_floor=-4.0,
        family="flat", short_description="Slight net downhill TT"),

    # Rolling (4)
    "rolling_easy": ArchetypeSpec(
        "rolling_easy", rolling_easy, 15.0, 50.0,
        smoothing_max_change=3.0, max_grade_cap=7.0, min_grade_floor=-6.0,
        family="rolling", short_description="Gentle rollers, no big kick"),
    "rolling_punchy": ArchetypeSpec(
        "rolling_punchy", rolling_punchy, 15.0, 45.0,
        smoothing_max_change=4.5, max_grade_cap=13.0, min_grade_floor=-9.0,
        family="rolling", short_description="Rolling + 2-4 short kickers"),
    "figure_8": ArchetypeSpec(
        "figure_8", figure_8, 15.0, 45.0,
        smoothing_max_change=3.0, max_grade_cap=8.0, min_grade_floor=-8.0,
        family="rolling", short_description="Figure-eight symmetric profile"),
    "rolling_with_climb_finish": ArchetypeSpec(
        "rolling_with_climb_finish", rolling_with_climb_finish, 20.0, 55.0,
        smoothing_max_change=3.5, max_grade_cap=10.0, min_grade_floor=-8.0,
        family="rolling", short_description="Rolling with summit climb finish"),

    # Climb (11)
    "wall": ArchetypeSpec(
        "wall", wall, 0.8, 2.5,
        smoothing_max_change=7.0, max_grade_cap=22.0, min_grade_floor=-6.0,
        family="climb", short_description="Flemish wall: avg 9-14%, peaks >18%"),
    "cat4_short": ArchetypeSpec(
        "cat4_short", cat4_short, 3.0, 8.0,
        smoothing_max_change=3.0, max_grade_cap=11.0, min_grade_floor=-3.0,
        family="climb", short_description="Short cat-4 climb"),
    "cat3_tempo": ArchetypeSpec(
        "cat3_tempo", cat3_tempo, 6.0, 10.0,
        smoothing_max_change=4.0, max_grade_cap=12.0, min_grade_floor=-3.0,
        family="climb", short_description="Cat-3 tempo climb"),
    "cat2_ramps": ArchetypeSpec(
        "cat2_ramps", cat2_ramps, 8.0, 14.0,
        smoothing_max_change=4.5, max_grade_cap=13.0, min_grade_floor=-4.0,
        family="climb", short_description="Cat-2 stepped ramps"),
    "cat1_sustained": ArchetypeSpec(
        "cat1_sustained", cat1_sustained, 12.0, 20.0,
        smoothing_max_change=4.5, max_grade_cap=12.0, min_grade_floor=-3.0,
        family="climb", short_description="Cat-1 sustained (alpine)"),
    "cat1_variable": ArchetypeSpec(
        "cat1_variable", cat1_variable, 8.0, 16.0,
        smoothing_max_change=5.0, max_grade_cap=14.0, min_grade_floor=-3.0,
        family="climb", short_description="Cat-1 variable (Galibier)"),
    "hc_steady": ArchetypeSpec(
        "hc_steady", hc_steady, 18.0, 28.0,
        smoothing_max_change=4.5, max_grade_cap=12.0, min_grade_floor=-3.0,
        family="climb", short_description="HC Gavia/Ventoux w/ plateaus"),
    "hc_irregular": ArchetypeSpec(
        "hc_irregular", hc_irregular, 18.0, 28.0,
        smoothing_max_change=6.0, max_grade_cap=18.0, min_grade_floor=-3.0,
        family="climb", short_description="HC Mortirolo-style irregular"),
    "two_stepper": ArchetypeSpec(
        "two_stepper", two_stepper, 15.0, 25.0,
        smoothing_max_change=4.5, max_grade_cap=12.0, min_grade_floor=-6.0,
        family="climb", short_description="Climb-descend-climb"),
    "summit_sprint": ArchetypeSpec(
        "summit_sprint", summit_sprint, 5.0, 12.0,
        smoothing_max_change=5.5, max_grade_cap=15.0, min_grade_floor=-3.0,
        family="climb", short_description="Mûr-de-Bretagne final kicker"),
    "false_flat_climb": ArchetypeSpec(
        "false_flat_climb", false_flat_climb, 10.0, 25.0,
        smoothing_max_change=1.2, max_grade_cap=5.0, min_grade_floor=-2.0,
        family="climb", short_description="Barely-noticeable drag"),

    # Cobble (4)
    "cobble_flat_classic": ArchetypeSpec(
        "cobble_flat_classic", cobble_flat_classic, 20.0, 60.0,
        smoothing_max_change=2.0, max_grade_cap=4.0, min_grade_floor=-3.0,
        family="cobble", short_description="Paris-Roubaix flat w/ cobble sectors"),
    "cobble_climb_muur": ArchetypeSpec(
        "cobble_climb_muur", cobble_climb_muur, 0.8, 2.5,
        smoothing_max_change=6.5, max_grade_cap=20.0, min_grade_floor=-2.0,
        family="cobble", short_description="Muur van Geraardsbergen wall"),
    "cobble_rolling": ArchetypeSpec(
        # Range bumped to 25-90 km and max_grade_cap to 20% so the
        # flanders_classic template can emit realistic Tour-of-Flanders
        # length routes with 20% Paterberg-style wall peaks.
        "cobble_rolling", cobble_rolling, 25.0, 90.0,
        smoothing_max_change=7.0, max_grade_cap=20.0, min_grade_floor=-8.0,
        family="cobble", short_description="Flemish classics (Ronde DNA)"),
    "cobble_finish": ArchetypeSpec(
        "cobble_finish", cobble_finish, 10.0, 20.0,
        smoothing_max_change=3.5, max_grade_cap=12.0, min_grade_floor=-4.0,
        family="cobble", short_description="Bologna: flat + cobble kicker"),

    # Gravel (5)
    "gravel_rolling_strade": ArchetypeSpec(
        "gravel_rolling_strade", gravel_rolling_strade, 30.0, 70.0,
        smoothing_max_change=4.5, max_grade_cap=13.0, min_grade_floor=-9.0,
        family="gravel", short_description="Strade Bianche rolling gravel"),
    "gravel_forest_rollercoaster": ArchetypeSpec(
        "gravel_forest_rollercoaster", gravel_forest_rollercoaster, 8.0, 20.0,
        smoothing_max_change=5.5, max_grade_cap=14.0, min_grade_floor=-12.0,
        family="gravel", short_description="Forest rollercoaster"),
    "gravel_climb_mountain": ArchetypeSpec(
        "gravel_climb_mountain", gravel_climb_mountain, 10.0, 20.0,
        smoothing_max_change=3.5, max_grade_cap=14.0, min_grade_floor=-4.0,
        family="gravel", short_description="Sustained mountain gravel"),
    "gravel_with_descent": ArchetypeSpec(
        "gravel_with_descent", gravel_with_descent, 15.0, 40.0,
        smoothing_max_change=4.0, max_grade_cap=10.0, min_grade_floor=-10.0,
        family="gravel", short_description="Gravel rolling + descent"),
    "gravel_adventure_long": ArchetypeSpec(
        "gravel_adventure_long", gravel_adventure_long, 60.0, 120.0,
        smoothing_max_change=4.5, max_grade_cap=11.0, min_grade_floor=-10.0,
        family="gravel", short_description="Epic mixed gravel"),

    # Mixed (2)
    "mixed_asphalt_gravel_sandwich": ArchetypeSpec(
        "mixed_asphalt_gravel_sandwich", mixed_asphalt_gravel_sandwich, 20.0, 50.0,
        smoothing_max_change=3.5, max_grade_cap=9.0, min_grade_floor=-8.0,
        family="mixed", short_description="Asphalt-gravel-asphalt sandwich"),
    "mixed_gravel_finish": ArchetypeSpec(
        "mixed_gravel_finish", mixed_gravel_finish, 15.0, 30.0,
        smoothing_max_change=3.5, max_grade_cap=12.0, min_grade_floor=-4.0,
        family="mixed", short_description="Asphalt + gravel summit finish"),

    # Lap (5)
    "lap_flat_tt": ArchetypeSpec(
        "lap_flat_tt", lap_flat_tt, 10.0, 40.0,
        smoothing_max_change=1.5, max_grade_cap=3.0, min_grade_floor=-3.0,
        family="lap", short_description="Flat TT laps"),
    "lap_rolling": ArchetypeSpec(
        "lap_rolling", lap_rolling, 12.0, 45.0,
        smoothing_max_change=3.5, max_grade_cap=8.0, min_grade_floor=-8.0,
        family="lap", short_description="Rolling laps"),
    "lap_climb": ArchetypeSpec(
        "lap_climb", lap_climb, 15.0, 40.0,
        smoothing_max_change=3.5, max_grade_cap=12.0, min_grade_floor=-12.0,
        family="lap", short_description="Climb+descent laps"),
    "lap_punchy_kicker": ArchetypeSpec(
        "lap_punchy_kicker", lap_punchy_kicker, 12.0, 40.0,
        smoothing_max_change=5.0, max_grade_cap=14.0, min_grade_floor=-6.0,
        family="lap", short_description="Flat+wall kicker laps"),
    "lap_criterium": ArchetypeSpec(
        "lap_criterium", lap_criterium, 8.0, 25.0,
        smoothing_max_change=1.5, max_grade_cap=3.0, min_grade_floor=-3.0,
        family="lap", short_description="Pure criterium"),
}

assert len(ARCHETYPE_REGISTRY) == 35, f"Expected 35 archetypes, have {len(ARCHETYPE_REGISTRY)}"


# ─────────────────────────────────────────────────────────────────────────────
# Self-validation
# ─────────────────────────────────────────────────────────────────────────────

def _validate_all(verbose: bool = True) -> dict:
    results = {}
    for name, spec in ARCHETYPE_REGISTRY.items():
        seed = (_hash32(1, ord(name[0]) * 7919) ^ len(name) * 31) & 0xFFFFFFFF
        dist = (spec.dist_min_km + spec.dist_max_km) / 2
        out = spec.fn(dist, seed)
        grades = apply_smoothing(out.grades, spec.smoothing_max_change)
        grades = apply_clipping(grades, spec.min_grade_floor, spec.max_grade_cap)
        total = sum(out.segs)
        climb = sum(d * 1000 * g / 100 for d, g in zip(out.segs, grades) if g > 0)
        max_g = max(grades) if grades else 0.0
        min_g = min(grades) if grades else 0.0
        avg_abs = sum(abs(g) for g in grades) / max(1, len(grades))
        sd = statistics.stdev(grades) if len(grades) > 1 else 0.0
        # Every segment should be 50m
        seg_mismatch = sum(1 for s in out.segs if abs(s - SEG_KM) > 0.001)
        results[name] = {
            "distance_km": round(total, 1),
            "climb_m": round(climb),
            "max_grade": round(max_g, 1),
            "min_grade": round(min_g, 1),
            "avg_grade_abs": round(avg_abs, 2),
            "sd": round(sd, 2),
            "n_surf_seg": len(out.surface_segments),
            "terrain": out.terrain,
            "finish": out.finish_type,
            "seg_mismatch": seg_mismatch,
            "template": out.template_id,
        }
        if verbose:
            print(f"  {name:36s}  d={total:5.1f}  climb={climb:4.0f}m  "
                  f"max={max_g:+5.1f}%  min={min_g:+5.1f}%  "
                  f"avg|{avg_abs:4.1f}%  SD={sd:4.1f}  tpl={out.template_id}")
        # Per-archetype: seg size universal 50m
        assert seg_mismatch == 0, f"{name} has {seg_mismatch} non-50m segments"
    max_grades = [r["max_grade"] for r in results.values()]
    sd_across = statistics.stdev(max_grades)
    assert sd_across >= 4.0, f"max_grade SD across archetypes = {sd_across:.2f} — too clustered!"
    # Check wall across multiple seeds — must hit >=15 on at least one draw
    wall_spec = ARCHETYPE_REGISTRY["wall"]
    wall_maxes = []
    for test_seed in range(16):
        out = wall_spec.fn(1.6, test_seed * 1337)
        grades = apply_smoothing(out.grades, wall_spec.smoothing_max_change)
        grades = apply_clipping(grades, wall_spec.min_grade_floor, wall_spec.max_grade_cap)
        wall_maxes.append(max(grades) if grades else 0.0)
    assert max(wall_maxes) >= 15.0, f"wall never steep enough across 16 seeds: {wall_maxes}"
    assert results["false_flat_climb"]["max_grade"] <= 6, "false_flat too steep"
    if verbose:
        print(f"\n  max_grade SD across archetypes = {sd_across:.2f}")
        print(f"  wall max across 16 seeds: {max(wall_maxes):.1f}%")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Shape-primitive validation (aperiodicity checks)
# ─────────────────────────────────────────────────────────────────────────────

def _max_autocorr(x: list[float], lags=(8, 12, 18, 25, 40, 60, 100)
                  ) -> float:
    """Maximum |autocorrelation| on the detrended, differenced signal.

    We care about **periodicity**, not smoothness. A sinusoidal grade
    profile has strong autocorrelation peaks at multiples of its period;
    an fBm/noise-driven profile decays toward 0 after a short lag.

    Approach: work on the first-difference of the series (removes DC and
    low-frequency trends), then compute autocorrelation of the residuals
    at a bank of lags. For a pure sinusoid the differenced signal is
    still periodic (max-autocorr ≈ 1). For aperiodic fBm it's close to
    white noise (max-autocorr small).

    Returns 0 for short or constant series.
    """
    if len(x) < 120:
        return 0.0
    # First-difference to remove trend / DC
    d = [x[i + 1] - x[i] for i in range(len(x) - 1)]
    m = statistics.mean(d)
    den = sum((v - m) ** 2 for v in d)
    if den == 0:
        return 0.0
    peaks: list[float] = []
    for lag in lags:
        n = len(d) - lag
        if n < 10:
            continue
        num = sum((d[i] - m) * (d[i + lag] - m) for i in range(n))
        peaks.append(abs(num / den))
    return max(peaks) if peaks else 0.0


def _validate_primitives(verbose: bool = True) -> dict:
    """Run 50 trials of each section kind, assert median max_autocorr < 0.55.

    A sinusoidal shape function will show autocorrelation ≈ 1; our aperiodic
    primitives should sit around 0.3–0.45.
    """
    results: dict = {}
    kinds_params = {
        "flat":             {"grade": 0.0},
        "rolling":          {"baseline": 0.0, "amp": 3.0, "period_km": 1.2},
        "short_hill":       {"peak": 7.0},
        "sustained_climb":  {"avg": 8.0, "roll_amp": 1.8},
        "gradual_climb":    {"avg": 6.0, "roll_amp": 1.3},
    }
    for kind, params in kinds_params.items():
        ac_peaks: list[float] = []
        for trial in range(50):
            # 8 km section at 50 m = 160 segments — enough for autocorr lags up to 100
            length_km = 8.0
            sec = Section(kind=kind, length_km=length_km, params=params)
            n = max(1, round(length_km / SEG_KM))
            grades = _shape_section(sec, n, seed=trial * 99991)
            ac_peaks.append(_max_autocorr(grades))
        median_ac = statistics.median(ac_peaks)
        worst = max(ac_peaks)
        results[kind] = {"median": median_ac, "worst": worst}
        if verbose:
            print(f"  {kind:<20s} median max_autocorr={median_ac:.3f}  "
                  f"worst={worst:.3f}")
        assert median_ac < 0.55, (
            f"{kind} is sinusoidal! median autocorr = {median_ac:.3f}"
        )
    return results


if __name__ == "__main__":
    import sys
    if "--validate-primitives" in sys.argv:
        _validate_primitives(True)
    else:
        _validate_all(True)
