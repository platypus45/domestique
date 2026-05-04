"""Ride analytics: ZWO workout parsing + post-hoc FIT-replay math.

v4.0.0-alpha: the live ride runtime (TrainingSession, BLE lifecycle, ERG/gate
controllers, first-pedal gate) was removed in the trainer-rip pivot. What
remains is pure math + parsers used by the planner, library browser, and
post-ride FIT-import viewer:

  * MetricsEngine — WP/NP, IF, TSS, W'bal, XPower, DFA alpha1, decoupling.
    Pure math; callers feed it power + HR samples and read properties.
  * CourseEngine — CRS position tracking, gradient lookup, surface lookup.
  * WorkoutEngine — ZWO segment tracking (no ERG wire-out; just target math).
  * RideRecorder — trivial sample-append buffer used by FIT-replay analyses.
  * FeedbackEngine — deviation / zone / W'bal banding helpers.
  * WarmupCooldownManager — ramp-target math for the workout library UI.
  * RidePhase enum — reduced to WARMUP/ROUTE/COOLDOWN/DONE.
  * parse_crs_for_session, parse_zwo_for_session, parse_zwo_tags,
    load_surface_segments — file parsers.
  * compute_virtual_speed + _sanitize_speed_inputs — cycling physics for
    post-hoc speed reconstruction when a FIT file lacks a speed column.

Nothing here opens a socket, reads a trainer, or writes wire bytes.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# v4.0.0-alpha: trimmed logger set — the BLE/gate/phase/trainer/hr/session
# named loggers are gone with the live runtime. `.power` is kept so
# post-hoc sanity-check warnings (grade out of envelope, bogus rider mass)
# still land under a structured category.
log_power = logging.getLogger("domestique.power")


# ══════════════════════════════════════════════════════════════════════════════
# HR-METRICS CANONICAL CONSTANTS (v3.6.0-fix25 hrm audit — still canonical)
# ══════════════════════════════════════════════════════════════════════════════
#
# Decoupling canonical filter + warmup trim + FIFO cap come from
# MASTER_DECISIONS_HRMETRICS.md §1.4 / §1.5 / §2. Shared between
# MetricsEngine (replay re-render) and fitness_estimation.aerobic_decoupling
# (post-hoc re-render) so both paths agree on one number.


def _is_valid_decoupling_sample(power_w: int | float, hr_bpm: int | float) -> bool:
    """Canonical Z1 filter for decoupling samples (§1.4).

    Accept only samples with 50 W <= power <= 2500 W (drop coasting + spikes)
    and 60 bpm <= HR <= 220 bpm (drop dropout and strap artifacts). Used by
    every decoupling code path so replay + post-hoc results agree.
    """
    try:
        p = float(power_w)
        h = float(hr_bpm)
    except (TypeError, ValueError):
        return False
    return 50.0 <= p <= 2500.0 and 60.0 <= h <= 220.0


# §1.5 warmup trim: drop the first 15 min of elapsed time before halving
# the ride. Configurable so future session-flag overrides can tune it.
DECOUPLING_WARMUP_TRIM_S: int = 900

# §1.5 minimum filtered duration after warmup trim (40 min effective).
DECOUPLING_MIN_FILTERED_S: int = 2400

# §2 FIFO buffer cap — 24000 entries ≈ 6.67 h at 1 Hz.
DECOUPLING_BUFFER_CAP: int = 24000

# §1.7 DFA alpha1 rolling-window horizon + buffer cap.
DFA_WINDOW_S: float = 120.0
DFA_BUFFER_CAP: int = 600
DFA_MIN_BEATS: int = 90
DFA_WARMUP_S: float = 180.0
DFA_GAP_THRESHOLD_S: float = 2.0
DFA_ARTIFACT_THRESHOLD: float = 5.0
DFA_UPDATE_INTERVAL_S: float = 5.0


def _dfa_zone_from_alpha(alpha1: float | None) -> tuple[str | None, str]:
    """Return (zone, color) per §1.9. Color drives the UI pill."""
    if alpha1 is None:
        return None, "gray"
    if alpha1 >= 1.00:
        return "aerobic", "green"
    if alpha1 >= 0.75:
        return "tempo", "yellow"
    return "threshold_or_above", "red"


# §DFA-6 rolling sparkline length: ~10 min of 5s updates = 120 entries.
DFA_SPARKLINE_MAXLEN: int = 120

# W'bal sparkline: 10 min of 5 s samples.
WBAL_SPARKLINE_MAXLEN: int = 120
WBAL_SPARKLINE_INTERVAL_S: float = 5.0

# Power-gap threshold for the W'bal integrator: if no valid power frame
# arrives for > this many seconds, skip the recovery tick so sensor gaps
# in an imported FIT do not inflate the bucket.
WBAL_POWER_GAP_S: float = 2.0

# Only emit a finite "time-to-empty" label once the athlete has held
# P > CP for at least this many seconds; shorter spikes flap the label.
WBAL_SUSTAIN_MIN_ABOVE_CP_S: float = 5.0


# ══════════════════════════════════════════════════════════════════════════════
# VIRTUAL SPEED CALCULATOR (cycling physics, used by FIT-replay speed fill-in)
# ══════════════════════════════════════════════════════════════════════════════

# Drivetrain efficiency: fraction of crank power that reaches the rear wheel
# after chain/pulley/bearing losses. 0.97 matches Zwift / Golden Cheetah.
DRIVETRAIN_EFFICIENCY: float = 0.97

# Sanity envelopes for diagnostic clamping of FIT-replay inputs.
GRADE_PCT_SANITY_MAX: float = 25.0
MASS_KG_SANITY_MIN: float = 30.0
MASS_KG_SANITY_MAX: float = 200.0
MASS_KG_DEFAULT: float = 83.0

# Default wind-resistance coefficient (kg/m) = rho * CdA. Historically sourced
# from the FTMS wire-format default; inlined here now that the trainer
# subsystem is gone. Equivalent to a road-bike rider in the hoods on a
# sea-level day. Override via the ``cw`` arg of ``compute_virtual_speed``.
DEFAULT_CW: float = 0.51

# Default rolling-resistance coefficient for indoor-trainer / asphalt.
DEFAULT_CRR: float = 0.004


def compute_virtual_speed(
    power_watts: float,
    grade_pct: float,
    rider_mass_kg: float,
    bike_mass_kg: float = 8.0,
    crr: float = DEFAULT_CRR,
    cw: float = DEFAULT_CW,
    prev_speed_mps: float = 0.0,
    dt: float = 1.0,
) -> float:
    """Compute virtual speed from power + gradient using cycling physics.

    ``cw`` is the wind-resistance coefficient (kg/m) = rho * CdA. The air
    density rho is already baked into Cw, so the aero term is
    ``0.5 * cw * v**2`` -- not ``0.5 * rho * cw * v**2`` (that would
    double-count rho).

    Crank power is multiplied by ``DRIVETRAIN_EFFICIENCY`` (0.97) before
    solving the balance -- only power reaching the rear wheel generates
    forward motion. Reference parity with Zwift / Golden Cheetah at
    identical FTP/CdA inputs.

    Uses Newton's method to solve the power balance equation:
        P_wheel = (F_gravity + F_rolling) * v + 0.5 * Cw * v**3

    On downhills (negative grade), gravity assists, producing positive
    speed even with zero power input (coasting). Returns km/h.
    """
    power_effective = float(power_watts) * DRIVETRAIN_EFFICIENCY

    M = rider_mass_kg + bike_mass_kg
    g = 9.81
    grade = grade_pct / 100.0
    theta = math.atan(grade)
    sin_t = math.sin(theta)
    cos_t = math.cos(theta)

    v = max(prev_speed_mps, 1.0)

    for _ in range(20):
        f_gravity = M * g * sin_t
        f_rolling = crr * M * g * cos_t
        f_aero = 0.5 * cw * v * v  # noqa: F841 (held for symmetry in docs)

        fv = power_effective - (f_gravity + f_rolling) * v - 0.5 * cw * v ** 3
        dfv = -(f_gravity + f_rolling) - 1.5 * cw * v ** 2

        if abs(dfv) < 1e-10:
            break
        v_new = v - fv / dfv
        v = max(0.0, v_new)
        if abs(fv) < 0.01:
            break

    return max(0.0, min(v * 3.6, 120.0))


def _sanitize_speed_inputs(
    grade_pct: float, rider_mass_kg: float
) -> tuple[float, float]:
    """Clamp grade + default mass before feeding physics.

    Returns (grade_clamped_pct, mass_used_kg). Emits WARN on log_power
    for any out-of-envelope input so post-mortem grep can catch bogus
    plumbing (e.g. grade=200 from a double-scaled fraction, mass=0
    from an uninitialised profile).
    """
    try:
        g = float(grade_pct)
    except (TypeError, ValueError):
        g = 0.0
    if abs(g) > GRADE_PCT_SANITY_MAX:
        log_power.warning(
            f"SANITY: grade_pct={g} out of "
            f"[-{GRADE_PCT_SANITY_MAX:.0f},{GRADE_PCT_SANITY_MAX:.0f}] -- clamping"
        )
        g = max(-GRADE_PCT_SANITY_MAX, min(GRADE_PCT_SANITY_MAX, g))
    try:
        m = float(rider_mass_kg)
    except (TypeError, ValueError):
        m = 0.0
    if not (MASS_KG_SANITY_MIN <= m <= MASS_KG_SANITY_MAX):
        log_power.warning(
            f"SANITY: rider_mass_kg={m} out of "
            f"[{MASS_KG_SANITY_MIN:.0f},{MASS_KG_SANITY_MAX:.0f}] -- "
            f"using default {MASS_KG_DEFAULT:.0f} kg"
        )
        m = MASS_KG_DEFAULT
    return g, m


import config  # noqa: E402  (import after constants to avoid circular lazy import)


# ══════════════════════════════════════════════════════════════════════════════
# ENUMS
# ══════════════════════════════════════════════════════════════════════════════

class SessionMode(Enum):
    FREE_RIDE = "free"
    COURSE = "course"
    WORKOUT = "workout"
    HYBRID = "hybrid"


class SegmentType(Enum):
    WARMUP = "Warmup"
    COOLDOWN = "Cooldown"
    STEADY_STATE = "SteadyState"
    INTERVALS_T = "IntervalsT"
    RAMP = "Ramp"
    FREE_RIDE = "FreeRide"


class RidePhase(Enum):
    """Top-level ride phase for the post-ride / replay viewer.

    v4.0.0-alpha reduced this enum to the three user-visible phases of a
    workout plus a terminal DONE. The legacy ARMED/INDEXING pre-recording
    gate was removed along with the live trainer runtime.
    """
    WARMUP = "warmup"
    ROUTE = "route"
    COOLDOWN = "cooldown"
    DONE = "done"


# ══════════════════════════════════════════════════════════════════════════════
# DATACLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RideSample:
    elapsed_sec: int
    power: int
    cadence: int
    speed: float
    hr: int
    distance_km: float
    elevation_m: float
    gradient_pct: float
    # Workout target watts AT THIS SAMPLE's record time. Zero when no
    # workout active. Used to compute compliance in summary() without
    # falling through to s.power (which would make compliance 100%).
    target_power: float = 0.0


@dataclass
class WorkoutSegment:
    seg_type: SegmentType
    start_sec: int
    duration: int
    power_low: int      # absolute watts
    power_high: int     # absolute watts
    repeats: int = 1
    on_duration: int = 0
    off_duration: int = 0
    on_power: int = 0
    off_power: int = 0


@dataclass
class IntervalState:
    segment_index: int
    segment_name: str
    target_power: int
    time_in_segment: int
    time_remaining: int
    current_rep: int = 0
    total_reps: int = 0
    is_work_phase: bool = True
    time_in_rep: int = 0
    rep_time_remaining: int = 0


@dataclass
class SurfaceSegment:
    start_km: float
    end_km: float
    surface: str
    intensity: int = 100


@dataclass
class RideSummary:
    duration_sec: int = 0
    distance_km: float = 0.0
    elevation_gain_m: float = 0.0
    avg_power: int = 0
    max_power: int = 0
    weighted_power: int = 0  # Equivalent to TrainingPeaks NP(R); renamed for trademark reasons
    intensity_factor: float = 0.0
    tss: float = 0.0
    avg_hr: int = 0
    max_hr: int = 0
    avg_cadence: int = 0
    avg_speed: float = 0.0
    # kj_mechanical: total mechanical work in kJ (NOT kcal -- field was
    # previously mislabeled "calories" but always stored kJ).
    kj_mechanical: int = 0
    wbal_min_kj: float = 0.0
    hr_zone_seconds: dict = field(default_factory=dict)
    power_zone_seconds: dict = field(default_factory=dict)
    course_name: str = ""
    workout_name: str = ""
    compliance_pct: float = 0.0
    dfa_alpha1_avg: float | None = None
    dfa_history: list = field(default_factory=list)
    total_kj: float = 0.0
    max_gradient: float = 0.0
    # Retained as optional fields so legacy ride JSONs still deserialize.
    hr_cap_ceiling_bpm: int | None = None
    hr_cap_time_capped_sec: int = 0
    hr_cap_avg_adjustment_w: int = 0
    decoupling_pct: float | None = None
    decoupling_reason: str | None = None
    efficiency_factor: float = 0.0

    @property
    def normalized_power(self) -> int:
        """Backward-compat alias for weighted_power."""
        return self.weighted_power

    @property
    def calories(self) -> int:
        """Backward-compat alias -- value is kJ of mechanical work, not kcal.
        Legacy name preserved because ride_storage.py / dashboard.html read it.
        Prefer ``kj_mechanical`` or ``kcal_estimate`` in new code.
        """
        return self.kj_mechanical

    @property
    def kcal_estimate(self) -> float:
        """Estimated dietary kcal from mechanical kJ.

        For cycling with gross efficiency ~0.24, metabolic kcal ~ kJ_mech
        (the J->kcal /4.184 and 1/0.239 factors cancel to ~1.0 when
        1/GE ~ 4.17).
        """
        return round(self.kj_mechanical / 4.184 / 0.239, 1)


# ══════════════════════════════════════════════════════════════════════════════
# METRICS ENGINE -- WP, IF, TSS, W'bal, DFA alpha1, decoupling
# ══════════════════════════════════════════════════════════════════════════════

class MetricsEngine:
    """Derived power + HR metrics. Pure: callers feed samples and read properties.

    Post-pivot, this class is used by the FIT-import post-ride viewer to
    replay a saved ride and surface the same analytics that used to be
    computed live. All O(1) per update.
    """

    def __init__(self, ftp: int, weight_kg: float):
        self.ftp = max(ftp, 1)
        self.weight = max(weight_kg, 1.0)
        # Critical Power (CP): McGrath et al. 2021 reports CP ~ 1.00-1.06 x FTP
        # (median ~1.03 x FTP). Profile CP overrides when present.
        from profile_manager import ProfileManager
        pm = ProfileManager.get()
        cp_from_profile = pm._athlete.get("cp")
        self._cp = int(cp_from_profile) if cp_from_profile else int(ftp * 1.03)
        # W' (anaerobic work capacity): McGrath 2021 median ~ 20 kJ at
        # FTP 250W -> 80 J per watt of FTP.
        wprime_from_profile = pm._athlete.get("wprime_j")
        self._wprime = (float(wprime_from_profile) if wprime_from_profile
                        else max(10000, ftp * 80.0))
        self._wbal = self._wprime
        self._wbal_min = self._wprime

        # Rolling 10-min W'bal sparkline.
        self._wbal_sparkline: deque[tuple[int, float]] = deque(
            maxlen=WBAL_SPARKLINE_MAXLEN
        )
        self._wbal_last_spark_s: float = -WBAL_SPARKLINE_INTERVAL_S

        # Power-drop gap tracking for the W'bal integrator.
        self._power_last_valid_mono: float = 0.0
        self._wbal_gap_skip_count: int = 0

        # "Time-to-empty at current P" predictive label guard.
        self._above_cp_run_s: float = 0.0
        self._wbal_sustain_s: Optional[int] = None

        # WP snapshot window + ride-wide Coggan NP accumulator.
        self._wp_window = deque(maxlen=30)
        self._rolling_power_window: deque = deque(maxlen=30)
        self._np_sum_p4: float = 0.0
        self._np_count: int = 0

        # XPower: 25s EWMA.
        self._xp_alpha = 2.0 / 26.0
        self._xp_ewma = 0.0
        self._xp_p4_sum = 0.0
        self._xp_count = 0

        # Running totals.
        self._power_sum = 0
        self._power_count = 0
        self._max_power = 0
        self._total_joules = 0.0
        self._elapsed = 0

        # Aerobic decoupling (§1.4 / §1.5).
        self._dc_powers: deque[int] = deque(maxlen=DECOUPLING_BUFFER_CAP)
        self._dc_hrs: deque[int] = deque(maxlen=DECOUPLING_BUFFER_CAP)
        self._dc_timestamps: deque[float] = deque(maxlen=DECOUPLING_BUFFER_CAP)
        self._dc_cached: dict = {}
        self._dc_cache_tick: int = -1
        self._dc_log_state: str | None = None
        self._dc_prev_hr: int = 0
        self._dc_hr_dropout_count: int = 0
        self._pw_hr_ratio_inst: float = 0.0

        # Live decoupling provisionality state (§DEC-2).
        self._decoupling_final: float | None = None
        self._decoupling_locked: bool = False
        self._indoor: bool = True

        # DFA alpha1 rolling state (§1.7/§1.8/§1.9).
        self._rr_buffer: deque[tuple[float, int]] = deque(maxlen=DFA_BUFFER_CAP)
        self._dfa_alpha1: float | None = None
        self._dfa_status: str = "stabilizing"
        self._dfa_artifact_pct: float = 0.0
        self._ectopic_artifact_carry: int = 0
        self._dfa_sparkline: deque[tuple[int, float]] = deque(maxlen=DFA_SPARKLINE_MAXLEN)
        self._dfa_history: list[tuple[int, float]] = []
        self._dfa_last_compute: float = 0.0
        self._dfa_zone: str | None = None
        self._dfa_color: str = "gray"
        self._session_recording: bool = True
        self._session_start_monotonic: float | None = None
        self._rr_warmup_until: float = 0.0

    def update(self, power: int, dt: float = 1.0, hr: int = 0) -> None:
        self._elapsed += dt
        self._power_sum += power
        self._power_count += 1
        if power > self._max_power:
            self._max_power = power
        self._total_joules += power * dt

        # WP snapshot + Coggan NP ride-wide accumulator.
        self._wp_window.append(power)
        self._rolling_power_window.append(power)
        if len(self._rolling_power_window) == 30:
            rolling_avg = sum(self._rolling_power_window) / 30
            self._np_sum_p4 += rolling_avg ** 4
            self._np_count += 1

        # XPower: 25s EWMA.
        self._xp_ewma = self._xp_alpha * power + (1 - self._xp_alpha) * self._xp_ewma
        self._xp_p4_sum += self._xp_ewma ** 4
        self._xp_count += 1

        # W'bal (Skiba 2015 differential model) with power-gap guard.
        now_mono = time.monotonic()
        is_valid_power = power > 0
        gap_skip = False
        if is_valid_power:
            self._power_last_valid_mono = now_mono
        elif self._power_last_valid_mono > 0:
            gap = now_mono - self._power_last_valid_mono
            if gap > WBAL_POWER_GAP_S:
                gap_skip = True
                self._wbal_gap_skip_count += 1

        if gap_skip:
            self._above_cp_run_s = 0.0
        else:
            if power > self._cp:
                self._wbal -= (power - self._cp) * dt
                self._above_cp_run_s += dt
            else:
                dcp = max(0, self._cp - power)
                tau = 546.0 * math.exp(-0.01 * dcp) + 316.0
                self._wbal += (self._wprime - self._wbal) * (1 - math.exp(-dt / tau))
                self._above_cp_run_s = 0.0
            self._wbal = max(0, min(self._wprime, self._wbal))
            if self._wbal < self._wbal_min:
                self._wbal_min = self._wbal

        # W'bal sparkline -- 5 s cadence, 10 min horizon.
        if not gap_skip and (
            self._elapsed - self._wbal_last_spark_s >= WBAL_SPARKLINE_INTERVAL_S
        ):
            self._wbal_sparkline.append(
                (int(self._elapsed), round(self._wbal / 1000.0, 2))
            )
            self._wbal_last_spark_s = self._elapsed

        # Sustain-time predictive label.
        if (
            not gap_skip
            and power > self._cp
            and self._above_cp_run_s >= WBAL_SUSTAIN_MIN_ABOVE_CP_S
            and self._wbal > 0
        ):
            drain = max(1, power - self._cp)
            self._wbal_sustain_s = int(self._wbal / drain)
        else:
            self._wbal_sustain_s = None

        # Aerobic decoupling sample intake (§1.4).
        dc_power = min(power, 2500)
        dc_valid = _is_valid_decoupling_sample(dc_power, hr)

        # HR dropout-recovery spike guard.
        if hr < 60:
            self._dc_hr_dropout_count += 1
        elif self._dc_hr_dropout_count > 0:
            if self._dc_prev_hr > 0 and abs(hr - self._dc_prev_hr) > 40:
                dc_valid = False
            self._dc_hr_dropout_count = max(0, self._dc_hr_dropout_count - 1)

        if dc_valid:
            self._dc_powers.append(dc_power)
            self._dc_hrs.append(hr)
            self._dc_timestamps.append(float(self._elapsed))
            self._pw_hr_ratio_inst = round(dc_power / hr, 3) if hr > 0 else 0.0
            self._dc_prev_hr = hr

    @property
    def wp(self) -> int:
        """Weighted Power (equivalent to TrainingPeaks NP(R))."""
        if self._np_count > 0:
            return round((self._np_sum_p4 / self._np_count) ** 0.25)
        if len(self._rolling_power_window) == 0:
            return 0
        return round(sum(self._rolling_power_window) / len(self._rolling_power_window))

    @property
    def np(self) -> int:
        """Backward-compat alias for wp (Weighted Power)."""
        return self.wp

    @property
    def xpower(self) -> int:
        if self._xp_count < 1:
            return 0
        return round((self._xp_p4_sum / self._xp_count) ** 0.25)

    @property
    def intensity_factor(self) -> float:
        return round(self.wp / self.ftp, 3)

    @property
    def tss(self) -> float:
        if_ = self.intensity_factor
        return round((self._elapsed * self.wp * if_) / (self.ftp * 3600) * 100, 1)

    @property
    def wbal(self) -> float:
        return self._wbal

    @property
    def wbal_pct(self) -> int:
        return round(self._wbal / self._wprime * 100) if self._wprime > 0 else 100

    @property
    def avg_power(self) -> int:
        return round(self._power_sum / self._power_count) if self._power_count else 0

    @property
    def max_power(self) -> int:
        return self._max_power

    @property
    def total_kj(self) -> float:
        return round(self._total_joules / 1000, 1)

    def ef_final(self) -> float:
        """Canonical Efficiency Factor (§1.6): NP / avg_HR (ride-aggregate)."""
        hrs = [h for h in self._dc_hrs if h > 0]
        if not hrs:
            return 0.0
        wp_final = self.wp
        avg_hr = sum(hrs) / len(hrs)
        if avg_hr <= 0:
            return 0.0
        return round(wp_final / avg_hr, 3)

    def _wp_for_slice(self, powers: list[int]) -> float:
        """Compute Weighted Power for a slice of power data."""
        if len(powers) < 30:
            return sum(powers) / len(powers) if powers else 0
        window = deque(maxlen=30)
        p4_sum = 0.0
        count = 0
        for p in powers:
            window.append(p)
            if len(window) == 30:
                avg30 = sum(window) / 30
                p4_sum += avg30 ** 4
                count += 1
        return (p4_sum / count) ** 0.25 if count > 0 else 0

    @property
    def decoupling(self) -> dict:
        """Aerobic decoupling (§1.5): Pw:Hr drift between first/second halves."""
        tick = self._elapsed
        if self._dc_cache_tick == tick and self._dc_cached:
            return self._dc_cached

        ef_ride = self.ef_final()
        trim_cutoff = float(DECOUPLING_WARMUP_TRIM_S)
        n_total = len(self._dc_timestamps)
        trimmed_idx = 0
        for i in range(n_total):
            if self._dc_timestamps[i] >= trim_cutoff:
                trimmed_idx = i
                break
        else:
            trimmed_idx = n_total

        filtered_powers = list(self._dc_powers)[trimmed_idx:]
        filtered_hrs = list(self._dc_hrs)[trimmed_idx:]
        filtered_ts = list(self._dc_timestamps)[trimmed_idx:]
        n_filtered = len(filtered_powers)

        if filtered_ts:
            filtered_span_s = filtered_ts[-1] - filtered_ts[0]
        else:
            filtered_span_s = 0.0

        if n_filtered < DECOUPLING_MIN_FILTERED_S or filtered_span_s < DECOUPLING_MIN_FILTERED_S:
            result = {
                "pct": None,
                "ef": ef_ride,
                "ef1": None,
                "ef2": None,
                "color": "gray",
                "reason": "ride_too_short",
            }
        else:
            mid = n_filtered // 2
            wp1 = self._wp_for_slice(filtered_powers[:mid])
            wp2 = self._wp_for_slice(filtered_powers[mid:])
            hrs_1 = filtered_hrs[:mid]
            hrs_2 = filtered_hrs[mid:]
            avg_hr1 = sum(hrs_1) / len(hrs_1) if hrs_1 else 0
            avg_hr2 = sum(hrs_2) / len(hrs_2) if hrs_2 else 0
            if avg_hr1 > 0 and avg_hr2 > 0:
                ef1 = wp1 / avg_hr1
                ef2 = wp2 / avg_hr2
                pct = round((ef1 - ef2) / ef1 * 100, 1) if ef1 > 0 else None
            else:
                ef1 = ef2 = 0.0
                pct = None
            if pct is None:
                color = "gray"
            elif pct < -3:
                color = "blue"
            elif abs(pct) < 5:
                color = "green"
            elif abs(pct) < 10:
                color = "yellow"
            else:
                color = "red"
            result = {
                "pct": pct,
                "ef": ef_ride,
                "ef1": round(ef1, 3),
                "ef2": round(ef2, 3),
                "color": color,
                "reason": None,
            }

        self._dc_cached = result
        self._dc_cache_tick = tick
        return result

    def lock_decoupling_final(self) -> dict:
        """Snapshot the canonical decoupling at stop (§DEC-2)."""
        dc = self.decoupling
        self._decoupling_final = dc["pct"]
        self._decoupling_locked = True
        try:
            n_total = len(self._dc_timestamps)
            trim_cutoff = float(DECOUPLING_WARMUP_TRIM_S)
            trimmed_idx = n_total
            for i in range(n_total):
                if self._dc_timestamps[i] >= trim_cutoff:
                    trimmed_idx = i
                    break
            n_filtered = max(0, n_total - trimmed_idx)
            half_n = n_filtered // 2
            new_state = "too_short" if dc.get("pct") is None else "ok"
            if self._dc_log_state != new_state:
                log_power.info(
                    f"EVENT=decoupling_computed method=np_per_half "
                    f"ef1={dc.get('ef1')} ef2={dc.get('ef2')} "
                    f"pct={dc.get('pct')} "
                    f"warmup_trimmed_s={int(DECOUPLING_WARMUP_TRIM_S)} "
                    f"half_n_samples={half_n} "
                    f"reason={dc.get('reason')}"
                )
                self._dc_log_state = new_state
        except Exception as e:
            log.debug(f"decoupling EVENT emission failed: {e}")
        return dc

    def set_indoor(self, indoor: bool) -> None:
        """Set the indoor flag (§DEC-6)."""
        self._indoor = bool(indoor)

    def set_recording_phase(self, recording: bool) -> None:
        """Gate alpha1 emission during stabilization / non-recording phases (§1.8)."""
        self._session_recording = bool(recording)

    # ── DFA alpha1 time-gated compute (§1.7 / §1.8 / §1.9) ─────────────────

    def add_rr_intervals(
        self,
        rr_list: (
            list[tuple[float, int]]
            | list[int]
            | None
        ) = None,
        rr_is_resync: bool = False,
        *,
        ectopic_corrected: int = 0,
        now_monotonic: float | None = None,
    ) -> None:
        """Ingest a batch of RR intervals.

        Canonical payload is ``list[tuple[monotonic_s, rr_ms]]``. Legacy
        ``list[int]`` (ms-only) is still accepted -- each entry is stamped
        with the current monotonic time.

        ``rr_is_resync=True`` flushes the window and arms a 180-s
        mini-warmup. Adjacent gaps > DFA_GAP_THRESHOLD_S drop the older
        prefix. ``ectopic_corrected`` accumulates into the current-window
        artifact counter so the >5% gate sees ectopic-only windows.
        """
        if not rr_list and not ectopic_corrected:
            return
        mono_now = float(time.monotonic()) if now_monotonic is None else float(now_monotonic)

        if rr_is_resync:
            self._rr_buffer.clear()
            self._rr_warmup_until = mono_now + DFA_WARMUP_S
            self._ectopic_artifact_carry = 0

        if ectopic_corrected:
            self._ectopic_artifact_carry += int(ectopic_corrected)

        for entry in (rr_list or []):
            if isinstance(entry, tuple) and len(entry) == 2:
                ts, rr = float(entry[0]), int(entry[1])
            else:
                ts, rr = mono_now, int(entry)
            if 300 <= rr <= 2000:
                self._rr_buffer.append((ts, rr))

        if self._rr_buffer:
            newest_ts = self._rr_buffer[-1][0]
            items = list(self._rr_buffer)
            last_gap_idx = 0
            saw_gap = False
            for i in range(1, len(items)):
                if items[i][0] - items[i - 1][0] > DFA_GAP_THRESHOLD_S:
                    last_gap_idx = i
                    saw_gap = True
            if saw_gap:
                self._rr_buffer.clear()
                for j in range(last_gap_idx, len(items)):
                    self._rr_buffer.append(items[j])
                self._rr_warmup_until = max(self._rr_warmup_until, newest_ts)

            cutoff = newest_ts - DFA_WINDOW_S
            while self._rr_buffer and self._rr_buffer[0][0] < cutoff:
                self._rr_buffer.popleft()

        if (self._elapsed - self._dfa_last_compute) >= DFA_UPDATE_INTERVAL_S:
            self._compute_dfa_alpha1(now_monotonic=mono_now)
            self._dfa_last_compute = float(self._elapsed)

    def _compute_dfa_alpha1(self, *, now_monotonic: float | None = None) -> None:
        """Compute DFA alpha1 over the current 120-s rolling window."""
        if now_monotonic is None:
            now_monotonic = float(time.monotonic())

        if self._session_start_monotonic is None:
            self._session_start_monotonic = now_monotonic
        session_elapsed = now_monotonic - self._session_start_monotonic

        in_mini_warmup = now_monotonic < self._rr_warmup_until

        if (not self._session_recording
                or session_elapsed < DFA_WARMUP_S
                or in_mini_warmup
                or self._elapsed < DFA_WARMUP_S):
            self._dfa_alpha1 = None
            self._dfa_status = "stabilizing"
            self._dfa_zone, self._dfa_color = _dfa_zone_from_alpha(None)
            return

        rr_entries = list(self._rr_buffer)
        rr = [r for _ts, r in rr_entries]
        n_beats = len(rr)
        if n_beats < DFA_MIN_BEATS:
            self._dfa_alpha1 = None
            self._dfa_status = "stabilizing"
            self._dfa_zone, self._dfa_color = _dfa_zone_from_alpha(None)
            return

        artifacts = 0
        clean_rr = []
        for i, r in enumerate(rr):
            lo = max(0, i - 5)
            hi = min(len(rr), i + 6)
            window = rr[lo:hi]
            median = sorted(window)[len(window) // 2]
            if median > 0 and abs(r - median) > 0.20 * median:
                artifacts += 1
                clean_rr.append(median)
            else:
                clean_rr.append(r)
        ectopic_n = self._ectopic_artifact_carry
        self._ectopic_artifact_carry = 0
        total_artifacts = artifacts + ectopic_n
        pct = (total_artifacts / n_beats * 100) if n_beats > 0 else 0.0
        self._dfa_artifact_pct = round(min(pct, 100.0), 1)
        if self._dfa_artifact_pct > DFA_ARTIFACT_THRESHOLD:
            self._dfa_alpha1 = None
            self._dfa_status = "artifacts"
            self._dfa_zone, self._dfa_color = _dfa_zone_from_alpha(None)
            return

        rr_mean = sum(clean_rr) / len(clean_rr)
        y = []
        cumsum = 0.0
        for r in clean_rr:
            cumsum += (r - rr_mean)
            y.append(cumsum)
        N = len(y)

        n_values: list[float] = []
        f_values: list[float] = []
        for n in range(4, 17):
            num_segs = N // n
            if num_segs < 2:
                continue
            fluct_sq = []
            for s in range(num_segs):
                seg = y[s * n:(s + 1) * n]
                x_mean = (n - 1) / 2.0
                y_mean_seg = sum(seg) / n
                num = sum((i - x_mean) * (seg[i] - y_mean_seg) for i in range(n))
                den = sum((i - x_mean) ** 2 for i in range(n))
                a = num / den if den > 0 else 0
                b = y_mean_seg - a * x_mean
                rms_sq = sum((seg[i] - (a * i + b)) ** 2 for i in range(n)) / n
                fluct_sq.append(rms_sq)
            if fluct_sq:
                f_n = math.sqrt(sum(fluct_sq) / len(fluct_sq))
                if f_n > 0:
                    n_values.append(math.log(n))
                    f_values.append(math.log(f_n))

        if len(n_values) < 3:
            self._dfa_alpha1 = None
            self._dfa_status = "unphysical"
            self._dfa_zone, self._dfa_color = _dfa_zone_from_alpha(None)
            return

        n_pts = len(n_values)
        x_mean = sum(n_values) / n_pts
        y_mean = sum(f_values) / n_pts
        num = sum((n_values[i] - x_mean) * (f_values[i] - y_mean) for i in range(n_pts))
        den = sum((n_values[i] - x_mean) ** 2 for i in range(n_pts))
        alpha1 = num / den if den > 0 else 0.0
        alpha1 = round(alpha1, 2)

        ss_res = sum(
            (f_values[i] - (y_mean + alpha1 * (n_values[i] - x_mean))) ** 2
            for i in range(n_pts)
        )
        ss_tot = sum((f_values[i] - y_mean) ** 2 for i in range(n_pts))
        r_sq = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
        if r_sq < 0.95:
            self._dfa_alpha1 = None
            self._dfa_status = "low_r2"
            self._dfa_zone, self._dfa_color = _dfa_zone_from_alpha(None)
            return

        if alpha1 < 0.30 or alpha1 > 1.60:
            self._dfa_alpha1 = None
            self._dfa_status = "unphysical"
            self._dfa_zone, self._dfa_color = _dfa_zone_from_alpha(None)
            return

        self._dfa_alpha1 = alpha1
        self._dfa_status = "ok"
        self._dfa_zone, self._dfa_color = _dfa_zone_from_alpha(alpha1)
        self._dfa_history.append((int(self._elapsed), alpha1))
        self._dfa_sparkline.append((int(self._elapsed), alpha1))

    @property
    def dfa_alpha1(self) -> dict:
        """DFA alpha1 broadcast dict for the post-ride viewer."""
        return {
            "alpha1": self._dfa_alpha1,
            "zone": self._dfa_zone,
            "color": self._dfa_color,
            "status": self._dfa_status,
            "artifact_pct": self._dfa_artifact_pct,
            "has_rr": len(self._rr_buffer) >= DFA_MIN_BEATS,
            "sparkline": list(self._dfa_sparkline),
            "history": list(self._dfa_sparkline)[-60:],
        }

    def snapshot(self) -> dict:
        dc = self.decoupling
        wp_val = self.wp
        if self._decoupling_locked:
            pct = self._decoupling_final
            provisional = False
        else:
            pct = dc["pct"]
            provisional = True
        return {
            "wp": wp_val, "np": wp_val,
            "xp": self.xpower, "if_": self.intensity_factor,
            "tss": self.tss, "wbal": round(self.wbal), "wbal_pct": self.wbal_pct,
            "avg_power": self.avg_power, "max_power": self.max_power,
            "total_kj": self.total_kj,
            "efficiency_factor": self.ef_final(),
            "decoupling_pct": pct,
            "decoupling_provisional": provisional,
            "decoupling_reason": dc.get("reason"),
            "decoupling_color": dc["color"],
            "decoupling_ef1": dc.get("ef1"),
            "decoupling_ef2": dc.get("ef2"),
            "is_indoor": self._indoor,
            "w_per_kg": round(wp_val / self.weight, 2),
            "dfa_alpha1": self._dfa_alpha1,
            "dfa_zone": self._dfa_zone,
            "dfa_status": self._dfa_status,
            "dfa_artifact_pct": self._dfa_artifact_pct,
            "dfa_has_rr": len(self._rr_buffer) >= DFA_MIN_BEATS,
            "dfa_sparkline": list(self._dfa_sparkline),
            "wbal_sparkline": list(self._wbal_sparkline),
            "wbal_sustain_s": self._wbal_sustain_s,
            "cp_w": int(self._cp),
            "wprime_j": int(self._wprime),
        }


# ══════════════════════════════════════════════════════════════════════════════
# COURSE ENGINE -- CRS position tracking + gradient lookup
# ══════════════════════════════════════════════════════════════════════════════
#
# Canonical surface map: MASTER_DECISIONS §1 locks the wire-format surface
# enum to the lowercase set {asphalt, gravel, cobble, dirt, sand, unknown}.
# The load_surface_segments() helper below also accepts uppercase aliases
# (legacy data).

_CANON_SURFACE_MAP: dict[str, str] = {
    "ASPHALT": "asphalt",
    "PAVED": "asphalt",
    "TARMAC": "asphalt",
    "GRAVEL": "gravel",
    "OFF_ROAD": "gravel",
    "COBBLESTONES_HARD": "cobble",
    "COBBLESTONES_SOFT": "cobble",
    "COBBLE": "cobble",
    "COBBLES": "cobble",
    "BRICK_ROAD": "cobble",
    "CONCRETE_PLATES": "cobble",
    "DIRT": "dirt",
    "TRAIL": "dirt",
    "SAND": "sand",
    "WOODEN_BOARDS": "unknown",
    "CATTLE_GRID": "unknown",
    "ICE": "unknown",
}


def _canonical_surface(raw) -> str:
    """Normalize any surface token to the canonical lowercase enum."""
    if not isinstance(raw, str) or not raw:
        return "unknown"
    lower = raw.strip().lower()
    if lower in {"asphalt", "gravel", "cobble", "dirt", "sand", "unknown"}:
        return lower
    return _CANON_SURFACE_MAP.get(raw.strip().upper(), "unknown")


class CourseEngine:
    """Tracks rider position on a CRS course. O(log n) gradient lookup.

    Pure math. Used by the post-ride viewer to replay position against
    the saved CRS course for a FIT import.
    """

    def __init__(self, points: list[dict], surfaces: list[SurfaceSegment] | None = None,
                 is_loop_course: bool = False):
        self._points = points
        self._distances = [p["d"] for p in points]
        self._surfaces = surfaces or []
        self._position = 0.0  # km
        self._is_loop_course = bool(is_loop_course)
        self._laps_completed = 0
        self._cache_position = -1.0
        self._cache_ahead = None
        self._cache_seg_prog = None
        self._cache_time = 0.0
        self._cache_ttl = 5.0

    @property
    def total_km(self) -> float:
        return self._distances[-1] if self._distances else 0

    @property
    def total_climb(self) -> float:
        gain = 0.0
        for i in range(1, len(self._points)):
            d = self._points[i]["e"] - self._points[i - 1]["e"]
            if d > 0:
                gain += d
        return round(gain, 1)

    @property
    def position_km(self) -> float:
        return self._position

    @property
    def progress_pct(self) -> float:
        return round(self._position / self.total_km * 100, 1) if self.total_km > 0 else 0

    def advance(self, speed_kmh: float, dt: float = 1.0) -> None:
        self._position += speed_kmh * dt / 3600
        total = self.total_km
        if self._is_loop_course and total > 0:
            while self._position >= total:
                self._position -= total
                self._laps_completed += 1
        else:
            self._position = min(self._position, total)

    @property
    def laps_completed(self) -> int:
        return self._laps_completed

    @property
    def is_loop_course(self) -> bool:
        return self._is_loop_course

    def _invalidate_cache(self) -> None:
        now = time.monotonic()
        if (abs(self._position - self._cache_position) > 0.02
                or now - self._cache_time > self._cache_ttl):
            self._cache_ahead = None
            self._cache_seg_prog = None

    def gradient_at(self, km: float) -> float:
        if not self._distances or km <= 0:
            return 0.0
        idx = bisect.bisect_right(self._distances, km) - 1
        idx = max(0, min(idx, len(self._points) - 1))
        return self._points[idx]["g"]

    def elevation_at(self, km: float) -> float:
        if not self._distances or km <= 0:
            return self._points[0]["e"] if self._points else 0
        idx = bisect.bisect_right(self._distances, km) - 1
        idx = max(0, min(idx, len(self._points) - 1))
        return self._points[idx]["e"]

    @property
    def current_gradient(self) -> float:
        return self.gradient_at(self._position)

    @property
    def current_elevation(self) -> float:
        return self.elevation_at(self._position)

    def gradient_ahead(self, lookahead_m: float = 500) -> list[dict]:
        self._invalidate_cache()
        if self._cache_ahead is not None:
            return self._cache_ahead
        result = []
        start = self._position
        for i in range(0, int(lookahead_m), 50):
            d = start + i / 1000.0
            if d > self.total_km:
                break
            result.append({"m": i, "g": round(self.gradient_at(d), 1)})
        self._cache_ahead = result
        self._cache_position = self._position
        self._cache_time = time.monotonic()
        return result

    def distance_to_summit(self) -> float | None:
        """Distance in km to next point where gradient goes negative."""
        for p in self._points:
            if p["d"] > self._position and p["g"] < -0.5:
                return round(p["d"] - self._position, 2)
        return None

    def current_surface(self) -> str:
        for s in self._surfaces:
            if s.start_km <= self._position < s.end_km:
                return _canonical_surface(s.surface)
        return "asphalt"

    @property
    def is_complete(self) -> bool:
        return self._position >= self.total_km - 0.01

    def segment_progress(self) -> float:
        self._invalidate_cache()
        if self._cache_seg_prog is not None:
            return self._cache_seg_prog
        cur_g = round(self.current_gradient)
        behind = 0
        pos = self._position
        while pos > 0:
            pos -= 0.05
            g = round(self.gradient_at(max(0, pos)))
            if abs(g - cur_g) <= 1:
                behind += 1
            else:
                break
            if behind > 40:
                break
        forward = 0
        pos = self._position
        while pos < self.total_km:
            pos += 0.05
            g = round(self.gradient_at(min(self.total_km, pos)))
            if abs(g - cur_g) <= 1:
                forward += 1
            else:
                break
            if forward > 40:
                break
        total = behind + forward
        result = behind / total if total > 0 else 0.0
        self._cache_seg_prog = result
        self._cache_position = self._position
        self._cache_time = time.monotonic()
        return result

    def snapshot(self) -> dict:
        return {
            "distance_km": round(self._position, 3),
            "total_km": self.total_km,
            "progress_pct": self.progress_pct,
            "gradient": round(self.current_gradient, 1),
            "elevation": round(self.current_elevation, 1),
            "surface": self.current_surface(),
            "summit_km": self.distance_to_summit(),
            "ahead": self.gradient_ahead(),
            "total_climb": self.total_climb,
            "segment_progress": round(self.segment_progress(), 2),
        }


# ══════════════════════════════════════════════════════════════════════════════
# WORKOUT ENGINE -- ZWO segment tracking
# ══════════════════════════════════════════════════════════════════════════════

class WorkoutEngine:
    """Tracks position within a ZWO structured workout."""

    def __init__(self, segments: list[WorkoutSegment], name: str = ""):
        self._segments = segments
        self._name = name
        self._elapsed = 0
        self._seg_idx = 0
        self._seg_elapsed = 0
        self._rep = 0
        self._rep_elapsed = 0
        self._is_work = True

    @property
    def total_duration(self) -> int:
        return sum(s.duration for s in self._segments)

    @property
    def elapsed(self) -> int:
        return self._elapsed

    def advance(self, dt: float = 1.0) -> None:
        if self.is_complete:
            return
        self._elapsed += int(dt)
        self._seg_elapsed += int(dt)

        seg = self._segments[self._seg_idx]

        if seg.seg_type == SegmentType.INTERVALS_T:
            self._rep_elapsed += int(dt)
            phase_dur = seg.on_duration if self._is_work else seg.off_duration
            if self._rep_elapsed >= phase_dur:
                self._rep_elapsed = 0
                if self._is_work:
                    self._is_work = False
                else:
                    self._is_work = True
                    self._rep += 1

        if self._seg_elapsed >= seg.duration:
            self._seg_elapsed = 0
            self._seg_idx += 1
            self._rep = 0
            self._rep_elapsed = 0
            self._is_work = True

    @property
    def current_target_power(self) -> int:
        if self.is_complete:
            return 0
        seg = self._segments[self._seg_idx]

        if seg.seg_type == SegmentType.INTERVALS_T:
            return seg.on_power if self._is_work else seg.off_power

        if seg.seg_type in (SegmentType.WARMUP, SegmentType.COOLDOWN, SegmentType.RAMP):
            frac = self._seg_elapsed / max(seg.duration, 1)
            return round(seg.power_low + (seg.power_high - seg.power_low) * frac)

        if seg.seg_type == SegmentType.FREE_RIDE:
            return 0

        return seg.power_high

    @property
    def is_free_ride_segment(self) -> bool:
        if self.is_complete:
            return True
        return self._segments[self._seg_idx].seg_type == SegmentType.FREE_RIDE

    @property
    def is_ramp_segment(self) -> bool:
        """True if the current segment is a ramp (warmup/cooldown/explicit ramp)."""
        if self.is_complete:
            return False
        return self._segments[self._seg_idx].seg_type in (
            SegmentType.WARMUP, SegmentType.COOLDOWN, SegmentType.RAMP)

    @property
    def is_complete(self) -> bool:
        return self._seg_idx >= len(self._segments)

    def interval_state(self) -> IntervalState | None:
        if self.is_complete:
            return None
        seg = self._segments[self._seg_idx]
        remaining = seg.duration - self._seg_elapsed
        rep_remaining = 0
        if seg.seg_type == SegmentType.INTERVALS_T:
            phase_dur = seg.on_duration if self._is_work else seg.off_duration
            rep_remaining = phase_dur - self._rep_elapsed

        return IntervalState(
            segment_index=self._seg_idx,
            segment_name=seg.seg_type.value,
            target_power=self.current_target_power,
            time_in_segment=self._seg_elapsed,
            time_remaining=max(0, remaining),
            current_rep=self._rep + 1,
            total_reps=seg.repeats,
            is_work_phase=self._is_work,
            time_in_rep=self._rep_elapsed,
            rep_time_remaining=max(0, rep_remaining),
        )

    def snapshot(self) -> dict:
        st = self.interval_state()
        if not st:
            return {"complete": True, "name": self._name}
        return {
            "name": self._name,
            "elapsed": self._elapsed,
            "total": self.total_duration,
            "progress_pct": round(self._elapsed / self.total_duration * 100, 1),
            "target_power": st.target_power,
            "segment_index": st.segment_index,
            "segment_count": len(self._segments),
            "segment_type": st.segment_name,
            "time_remaining": st.time_remaining,
            "rep": st.current_rep,
            "total_reps": st.total_reps,
            "is_work": st.is_work_phase,
            "rep_remaining": st.rep_time_remaining,
            "is_free_ride": self.is_free_ride_segment,
            "complete": False,
        }


# ══════════════════════════════════════════════════════════════════════════════
# RIDE RECORDER -- sample storage
# ══════════════════════════════════════════════════════════════════════════════

class RideRecorder:
    """Stores 1Hz ride samples for post-ride analysis and FIT export."""

    def __init__(self):
        self._samples: list[RideSample] = []

    def record(self, sample: RideSample) -> None:
        self._samples.append(sample)

    @property
    def samples(self) -> list[RideSample]:
        return self._samples

    @property
    def count(self) -> int:
        return len(self._samples)


# ══════════════════════════════════════════════════════════════════════════════
# FEEDBACK ENGINE -- countdowns + deviation alerts (used by post-ride viewer)
# ══════════════════════════════════════════════════════════════════════════════

class FeedbackEngine:
    """Generates real-time alerts: countdowns, deviation, W'bal warnings."""

    def __init__(self, ftp: int, lthr: int, max_hr: int):
        self.ftp = ftp
        self._hr_zones = _build_hr_zones(lthr, max_hr)
        self._prev_seg_idx = -1
        self._deviation_streak = 0

    def update(self, power: int, hr: int, interval: IntervalState | None,
               wbal_pct: float) -> list[dict]:
        alerts = []

        if interval and interval.time_remaining <= 5 and interval.time_remaining > 0:
            alerts.append({"type": "countdown", "value": interval.time_remaining})

        if interval and interval.segment_index != self._prev_seg_idx:
            if self._prev_seg_idx >= 0:
                alerts.append({"type": "segment_start", "name": interval.segment_name,
                               "target": interval.target_power})
            self._prev_seg_idx = interval.segment_index

        if interval and interval.target_power > 0:
            dev_pct = abs(power - interval.target_power) / interval.target_power * 100
            if dev_pct > 10:
                self._deviation_streak += 1
                if self._deviation_streak >= 5:
                    alerts.append({"type": "deviation", "delta_w": power - interval.target_power,
                                   "pct": round(dev_pct, 1)})
            else:
                self._deviation_streak = 0

        if wbal_pct < 15:
            alerts.append({"type": "wbal_critical", "pct": wbal_pct})
        elif wbal_pct < 25:
            alerts.append({"type": "wbal_warning", "pct": wbal_pct})

        return alerts

    def power_deviation(self, actual: int, target: int) -> dict:
        if target <= 0:
            return {"delta_w": 0, "pct": 0, "color": "green"}
        delta = actual - target
        pct = abs(delta) / target * 100
        color = "green" if pct <= 5 else "yellow" if pct <= 10 else "red"
        return {"delta_w": delta, "pct": round(pct, 1), "color": color}

    def hr_zone(self, hr: int) -> dict:
        for z in reversed(self._hr_zones):
            if hr >= z["low"]:
                return z
        return self._hr_zones[0] if self._hr_zones else {"zone": "Z1", "name": "Recovery", "low": 0, "high": 0}

    def power_zone(self, power: int) -> dict:
        zones = _build_power_zones(self.ftp)
        for z in reversed(zones):
            if power >= z["low"]:
                return z
        return zones[0] if zones else {"zone": "Z1", "name": "Recovery", "low": 0, "high": 0}


# ══════════════════════════════════════════════════════════════════════════════
# WARMUP / COOLDOWN MANAGER -- ramp-target math for the workout UI
# ══════════════════════════════════════════════════════════════════════════════

class WarmupCooldownManager:
    """Manages warmup/cooldown phase transitions with optional HR-based detection.

    Still useful post-pivot: the workout library UI uses it to precompute
    ramp targets when previewing a workout. No live-trainer coupling.
    """

    def __init__(self, warmup_sec: int, cooldown_sec: int, ftp: int, lthr: int,
                 auto_detect: bool = False):
        self._warmup_sec = warmup_sec
        self._cooldown_sec = cooldown_sec
        self._ftp = max(ftp, 1)
        self._lthr = max(lthr, 1)
        self._auto_detect = auto_detect

        self._phase = RidePhase.WARMUP if warmup_sec > 0 else RidePhase.ROUTE
        self._phase_start_sec = 0
        self._elapsed = 0

        self._hr_above_threshold_streak = 0
        self._warmup_hr_threshold = round(lthr * 0.75)
        self._cooldown_hr_threshold = round(lthr * 0.60)

    def update(self, elapsed_sec: int, hr: int, power: int) -> dict:
        self._elapsed = elapsed_sec
        phase_elapsed = elapsed_sec - self._phase_start_sec

        if hr >= self._warmup_hr_threshold:
            self._hr_above_threshold_streak += 1
        else:
            self._hr_above_threshold_streak = 0

        hr_pct_lthr = round(hr / self._lthr * 100, 1) if self._lthr > 0 else 0

        if self._phase == RidePhase.WARMUP and not self._auto_detect:
            if phase_elapsed >= self._warmup_sec:
                self._phase = RidePhase.ROUTE
                self._phase_start_sec = elapsed_sec

        if self._phase == RidePhase.COOLDOWN:
            if self._cooldown_sec > 0 and phase_elapsed >= self._cooldown_sec:
                self._phase = RidePhase.DONE
                self._phase_start_sec = elapsed_sec

        target_power = self._compute_target_power(phase_elapsed)
        progress_pct = self._compute_progress(phase_elapsed)

        return {
            "phase": self._phase.value,
            "target_power": target_power,
            "progress_pct": progress_pct,
            "warmup_ready": self.is_warmed_up(),
            "hr_pct_lthr": hr_pct_lthr,
            "can_extend": self._phase == RidePhase.WARMUP,
            "can_skip": self._phase == RidePhase.WARMUP,
        }

    def _compute_target_power(self, phase_elapsed: int) -> int:
        if self._phase == RidePhase.WARMUP:
            duration = self._warmup_sec if self._warmup_sec > 0 else 300
            frac = min(phase_elapsed / duration, 1.0)
            low = self._ftp * 0.50
            high = self._ftp * 0.75
            return round(low + (high - low) * frac)
        elif self._phase == RidePhase.COOLDOWN:
            duration = self._cooldown_sec if self._cooldown_sec > 0 else 300
            frac = min(phase_elapsed / duration, 1.0)
            high = self._ftp * 0.75
            low = self._ftp * 0.40
            return round(high + (low - high) * frac)
        return 0

    def _compute_progress(self, phase_elapsed: int) -> float:
        if self._phase == RidePhase.WARMUP:
            if self._warmup_sec > 0:
                return round(min(phase_elapsed / self._warmup_sec * 100, 100.0), 1)
            return 100.0
        elif self._phase == RidePhase.COOLDOWN:
            if self._cooldown_sec > 0:
                return round(min(phase_elapsed / self._cooldown_sec * 100, 100.0), 1)
            return 0.0
        elif self._phase == RidePhase.ROUTE:
            return 0.0
        return 100.0

    def extend_warmup(self, extra_sec: int = 300) -> None:
        if self._phase == RidePhase.WARMUP:
            self._warmup_sec += extra_sec

    def skip_to_route(self) -> None:
        if self._phase == RidePhase.WARMUP:
            self._phase = RidePhase.ROUTE
            self._phase_start_sec = self._elapsed

    def start_cooldown(self, duration_sec: int = 300) -> None:
        if self._phase == RidePhase.ROUTE:
            self._cooldown_sec = duration_sec
            self._phase = RidePhase.COOLDOWN
            self._phase_start_sec = self._elapsed
            self._hr_above_threshold_streak = 0

    def is_warmed_up(self) -> bool:
        if self._phase != RidePhase.WARMUP:
            return True
        if self._auto_detect:
            return self._hr_above_threshold_streak >= 30
        phase_elapsed = self._elapsed - self._phase_start_sec
        return phase_elapsed >= self._warmup_sec

    def is_cooled_down(self) -> bool:
        if self._phase == RidePhase.DONE:
            return True
        if self._phase != RidePhase.COOLDOWN:
            return False
        phase_elapsed = self._elapsed - self._phase_start_sec
        if self._cooldown_sec > 0 and phase_elapsed >= self._cooldown_sec:
            return True
        return self._hr_above_threshold_streak == 0 and phase_elapsed > 0

    @property
    def phase(self) -> RidePhase:
        return self._phase

    @property
    def phase_elapsed(self) -> int:
        return self._elapsed - self._phase_start_sec

    @property
    def phase_remaining(self) -> int:
        elapsed_in_phase = self._elapsed - self._phase_start_sec
        if self._phase == RidePhase.WARMUP:
            if self._auto_detect and elapsed_in_phase >= self._warmup_sec:
                return 0
            return max(0, self._warmup_sec - elapsed_in_phase)
        elif self._phase == RidePhase.COOLDOWN:
            return max(0, self._cooldown_sec - elapsed_in_phase)
        return 0

    def snapshot(self) -> dict:
        phase_elapsed = self._elapsed - self._phase_start_sec
        return {
            "phase": self._phase.value,
            "phase_elapsed": phase_elapsed,
            "phase_remaining": self.phase_remaining,
            "target_power": self._compute_target_power(phase_elapsed),
            "progress_pct": self._compute_progress(phase_elapsed),
            "warmup_ready": self.is_warmed_up(),
            "cooled_down": self.is_cooled_down(),
            "warmup_sec": self._warmup_sec,
            "cooldown_sec": self._cooldown_sec,
            "auto_detect": self._auto_detect,
        }


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS -- zone builders, CRS/ZWO parsers, surface loader
# ══════════════════════════════════════════════════════════════════════════════

def _build_hr_zones(lthr: int, max_hr: int) -> list[dict]:
    """Friel 5-zone HR model. Delegates to canonical ``zones.hr_zones``."""
    import zones as _zones
    return [
        {"zone": f"Z{i}", "name": z.name, "low": z.low, "high": z.high}
        for i, z in enumerate(_zones.hr_zones(lthr, max_hr), start=1)
    ]


def _build_power_zones(ftp: int) -> list[dict]:
    """Coggan 7-zone power model. Delegates to canonical ``zones.power_zones``."""
    import zones as _zones
    return [
        {"zone": f"Z{i}", "name": z.name, "low": z.low, "high": z.high}
        for i, z in enumerate(_zones.power_zones(ftp), start=1)
    ]


def parse_crs_for_session(crs_path: Path, skip_warmup_km: float = 0.0,
                          skip_cooldown_km: float = 0.0) -> list[dict]:
    """Parse a CRS file into [{d: cumulative_km, g: grade%, e: elevation_m}, ...].

    Handles the delta-distance format; auto-detects fraction-vs-percent grade.
    Bails on malformed rows rather than raising.
    """
    def _read_lines(path: Path) -> list[str]:
        try:
            with open(path, encoding="utf-8-sig") as fh:
                return fh.readlines()
        except UnicodeDecodeError:
            log.warning("CRS %s not UTF-8; retrying with latin-1", path)
            with open(path, encoding="latin-1") as fh:
                return fh.readlines()

    points = []
    in_data = False
    bad_lines = 0
    total_data_lines = 0
    for lineno, line in enumerate(_read_lines(crs_path), start=1):
        s = line.strip()
        if s == "[COURSE DATA]":
            in_data = True
            continue
        if s == "[END COURSE DATA]":
            break
        if in_data and s and not s.startswith("DISTANCE"):
            total_data_lines += 1
            parts = s.split()
            if len(parts) >= 2:
                try:
                    points.append({"delta": float(parts[0]), "g": float(parts[1])})
                except ValueError:
                    bad_lines += 1
                    log.warning(
                        "CRS %s line %d: malformed numeric fields (%r)",
                        crs_path, lineno, s,
                    )
            else:
                bad_lines += 1
                log.warning(
                    "CRS %s line %d: too few fields (%r)",
                    crs_path, lineno, s,
                )

    if total_data_lines and bad_lines / total_data_lines > 0.10:
        log.error(
            "CRS %s rejected: %d/%d malformed lines (>10%%)",
            crs_path, bad_lines, total_data_lines,
        )
        return [{"d": 0.0, "g": 0.0, "e": 0.0}]

    if points:
        max_abs_g = max(abs(p.get("g", 0.0)) for p in points)
        if 0 < max_abs_g <= 1.0:
            log.info(
                "CRS %s: fraction-format grade detected (max|g|=%.3f); "
                "scaling by x100 to percent",
                crs_path, max_abs_g,
            )
            for p in points:
                p["g"] = p.get("g", 0.0) * 100.0

    if points and points[0].get("delta", 0) > 0:
        start_iter = points
    else:
        start_iter = points[1:]

    result = [{"d": 0.0, "g": 0.0, "e": 0.0}]
    cum_d = 0.0
    cum_e = 0.0
    for p in start_iter:
        cum_d += p["delta"]
        cum_e += p["delta"] * 10 * p["g"]
        result.append({"d": round(cum_d, 3), "g": p["g"], "e": round(cum_e, 1)})

    if skip_warmup_km > 0 and result:
        trimmed = [p for p in result if p["d"] >= skip_warmup_km]
        if len(trimmed) < 2:
            log.warning(
                "CRS %s: skip_warmup_km=%.2f would leave <2 points; keeping untrimmed",
                crs_path, skip_warmup_km,
            )
        else:
            offset_d = trimmed[0]["d"]
            offset_e = trimmed[0]["e"]
            for p in trimmed:
                p["d"] = round(p["d"] - offset_d, 3)
                p["e"] = round(p["e"] - offset_e, 1)
            result = trimmed

    if skip_cooldown_km > 0 and result:
        total_d = result[-1]["d"]
        cutoff = total_d - skip_cooldown_km
        if cutoff > 0:
            trimmed = [p for p in result if p["d"] <= cutoff]
            if len(trimmed) < 2:
                log.warning(
                    "CRS %s: skip_cooldown_km=%.2f would leave <2 points; keeping untrimmed",
                    crs_path, skip_cooldown_km,
                )
            else:
                result = trimmed
        else:
            log.warning(
                "CRS %s: skip_cooldown_km=%.2f exceeds course length %.2fkm; keeping untrimmed",
                crs_path, skip_cooldown_km, total_d,
            )

    return result


# Surface-segment loader. Keys map raw JSON tokens to canonical enum values
# via ``_canonical_surface``. The table below preserves the legacy
# UPPERCASE/alias tokens found in older surface_types.json files.
_SURFACE_ALIAS_MAP: dict[str, str] = {
    "ASPHALT": "asphalt",
    "COBBLESTONES_HARD": "cobble",
    "COBBLESTONES_SOFT": "cobble",
    "GRAVEL": "gravel",
    "OFF_ROAD": "gravel",
    "WOODEN_BOARDS": "unknown",
    "BRICK_ROAD": "cobble",
    "DIRT": "dirt",
    "CONCRETE_PLATES": "cobble",
    "CATTLE_GRID": "unknown",
    "ICE": "unknown",
}


def load_surface_segments(route_id: str) -> list[SurfaceSegment]:
    """Load surface segments for a course from surface_types.json.

    ``route_id`` is the canonical ``"<region>/<slug>"`` key used in
    ``surface_types.json``. Returns an empty list when the file is
    absent or the id is unknown.
    """
    db_path = Path(__file__).parent / "surface_types.json"
    if not db_path.exists():
        return []
    try:
        with open(db_path, encoding="utf-8") as f:
            db = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    raw_segments = db.get(route_id, []) or []
    if not raw_segments:
        return []

    segments = []
    for seg in raw_segments:
        raw_surface = seg.get("surface", "ASPHALT").upper()
        surface_name = _SURFACE_ALIAS_MAP.get(raw_surface, "asphalt")
        segments.append(SurfaceSegment(
            start_km=seg.get("start_km", 0.0),
            end_km=seg.get("end_km", 0.0),
            surface=surface_name,
            intensity=seg.get("intensity", 100),
        ))
    return segments


def parse_zwo_tags(zwo_path: Path) -> set[str]:
    """Extract ``<tag name="..."/>`` tokens from a ZWO's <tags> block.

    Used by the library filter. Returns an empty set on parse failure
    or missing block.
    """
    try:
        tree = ET.parse(zwo_path)
    except (ET.ParseError, OSError):
        return set()
    root = tree.getroot()
    tags_el = root.find("tags")
    if tags_el is None:
        return set()
    out: set[str] = set()
    for t in tags_el.findall("tag"):
        nm = (t.get("name") or "").strip().lower()
        if nm:
            out.add(nm)
    return out


def parse_zwo_for_session(zwo_path: Path, ftp: int) -> tuple[str, list[WorkoutSegment]]:
    """Parse a ZWO file into WorkoutSegment objects with absolute power values.

    Returns ``(name, segments)``. Unknown tags are warned once per file.
    """
    try:
        tree = ET.parse(zwo_path)
    except ET.ParseError:
        return zwo_path.stem, []

    root = tree.getroot()
    name = (root.findtext("name") or zwo_path.stem).strip()
    workout_el = root.find("workout")
    if workout_el is None:
        return name, []

    segments = []
    cum_sec = 0
    _known_zwo_tags = {"Warmup", "Cooldown", "Ramp", "SteadyState", "IntervalsT", "FreeRide"}
    _unknown_seen: set[str] = set()
    for el in workout_el:
        tag = el.tag
        dur = int(el.get("Duration", 0))
        plo = float(el.get("PowerLow", el.get("Power", 0.65)))
        phi = float(el.get("PowerHigh", el.get("Power", 0.65)))

        if tag in ("Warmup", "Cooldown", "Ramp"):
            seg = WorkoutSegment(
                seg_type=SegmentType(tag), start_sec=cum_sec, duration=dur,
                power_low=round(plo * ftp), power_high=round(phi * ftp))
            segments.append(seg)
            cum_sec += dur

        elif tag == "SteadyState":
            pw = float(el.get("Power", 0.65))
            seg = WorkoutSegment(
                seg_type=SegmentType.STEADY_STATE, start_sec=cum_sec, duration=dur,
                power_low=round(pw * ftp), power_high=round(pw * ftp))
            segments.append(seg)
            cum_sec += dur

        elif tag == "IntervalsT":
            reps = int(el.get("Repeat", 1))
            on_dur = int(el.get("OnDuration", 0))
            off_dur = int(el.get("OffDuration", 0))
            on_pwr = float(el.get("OnPower", 0.95))
            off_pwr = float(el.get("OffPower", 0.50))
            total = reps * (on_dur + off_dur)
            seg = WorkoutSegment(
                seg_type=SegmentType.INTERVALS_T, start_sec=cum_sec, duration=total,
                power_low=round(off_pwr * ftp), power_high=round(on_pwr * ftp),
                repeats=reps, on_duration=on_dur, off_duration=off_dur,
                on_power=round(on_pwr * ftp), off_power=round(off_pwr * ftp))
            segments.append(seg)
            cum_sec += total

        elif tag == "FreeRide":
            seg = WorkoutSegment(
                seg_type=SegmentType.FREE_RIDE, start_sec=cum_sec, duration=dur,
                power_low=0, power_high=0)
            segments.append(seg)
            cum_sec += dur

        else:
            if tag not in _unknown_seen:
                _unknown_seen.add(tag)
                log.warning(
                    "ZWO %s: unknown tag <%s> ignored (no segment emitted)",
                    zwo_path.name, tag,
                )

    return name, segments
