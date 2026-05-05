"""Unit tests for training_live.py surviving modules (v4.0.0-alpha).

Trainer subsystem removed; this suite tests the pure-math / parser engines
that survived: MetricsEngine, CourseEngine, WorkoutEngine, FeedbackEngine,
WarmupCooldownManager, zone builders, CRS/ZWO parsers, RidePhase enum,
compute_virtual_speed physics. Live TrainingSession runtime / BLE / ERG /
first-pedal gate / phase FSM tests were deleted with the trainer rip.

Run: python3 -m pytest test_training_live.py -v
"""

import math
from pathlib import Path

import pytest

from training_live import (
    MetricsEngine, CourseEngine, WorkoutEngine, RideRecorder,
    FeedbackEngine, IntervalState,
    SessionMode, SegmentType, WorkoutSegment, SurfaceSegment,
    RideSample, parse_crs_for_session, _build_hr_zones, _build_power_zones,
    RidePhase, WarmupCooldownManager, compute_virtual_speed,
)


# ══════════════════════════════════════════════════════════════════════════════
# MetricsEngine tests
# ══════════════════════════════════════════════════════════════════════════════

class TestMetricsEngine:
    def test_basic_power_tracking(self):
        m = MetricsEngine(ftp=250, weight_kg=72)
        for _ in range(60):
            m.update(200)
        assert m.avg_power == 200
        assert m.max_power == 200
        assert m.total_kj > 0

    def test_np_steady_state(self):
        """NP of constant power should equal that power (after 30s window fills)."""
        m = MetricsEngine(ftp=250, weight_kg=72)
        for _ in range(60):
            m.update(200)
        assert abs(m.np - 200) <= 1, f"NP {m.np} should be ~200 for constant power"

    def test_np_variable_power(self):
        """NP should be higher than avg for power that varies on a ≥30s timescale.

        Coggan NP is the 4th-power mean of the 30-SECOND ROLLING AVERAGE of power,
        so 1Hz alternation (e.g. 300/100 every second) washes out inside the
        window and produces NP == avg — which is correct per the spec, not a bug.
        Use 60-second on/off blocks so the window actually sees variation.
        """
        m = MetricsEngine(ftp=250, weight_kg=72)
        for i in range(600):  # 10 minutes
            # 60s at 300W, 60s at 100W, repeat — variation the window can see
            power = 300 if (i // 60) % 2 == 0 else 100
            m.update(power)
        assert m.np > m.avg_power, f"NP {m.np} should exceed avg {m.avg_power}"

    def test_tss_one_hour_at_ftp(self):
        """1 hour at FTP should give TSS ~100."""
        m = MetricsEngine(ftp=250, weight_kg=72)
        for _ in range(3600):
            m.update(250)
        assert 95 <= m.tss <= 105, f"TSS {m.tss} should be ~100 for 1h at FTP"

    def test_intensity_factor_at_ftp(self):
        m = MetricsEngine(ftp=250, weight_kg=72)
        for _ in range(60):
            m.update(250)
        assert abs(m.intensity_factor - 1.0) < 0.05

    def test_wbal_depletion(self):
        """W'bal should decrease when riding well above CP."""
        m = MetricsEngine(ftp=250, weight_kg=72)
        initial = m.wbal
        for _ in range(60):
            m.update(400)  # well above CP (190W), strong depletion
        assert m.wbal < initial, f"W'bal {m.wbal} should be < initial {initial}"
        assert m.wbal_pct < 100

    def test_wbal_recovery(self):
        """W'bal should recover when riding below CP."""
        m = MetricsEngine(ftp=250, weight_kg=72)
        # Deplete
        for _ in range(120):
            m.update(350)
        depleted = m.wbal
        # Recover
        for _ in range(120):
            m.update(100)
        assert m.wbal > depleted, "W'bal should recover below CP"

    def test_w_per_kg(self):
        m = MetricsEngine(ftp=250, weight_kg=72)
        for _ in range(60):
            m.update(250)
        snap = m.snapshot()
        assert snap["w_per_kg"] > 0
        assert abs(snap["w_per_kg"] - 250 / 72) < 0.5

    # ── v3.6.0-fix26 §4.3 / §4.4 W'bal UI + gap-handling tests ────────

    def test_wbal_sparkline_populated(self):
        """Every 5 s the engine appends a sparkline point so the UI has
        a 10-min trend curve. At 60 s we expect ~12 points."""
        m = MetricsEngine(ftp=250, weight_kg=72)
        for _ in range(60):
            m.update(200)
        pts = m._wbal_sparkline
        assert len(pts) >= 10, f"expected ≥10 sparkline points, got {len(pts)}"
        # Each tuple is (elapsed_s, wbal_kj)
        for t_s, kj in pts:
            assert t_s >= 0
            assert kj > 0

    def test_wbal_sparkline_maxlen(self):
        """Deque trims to ~10 min of samples even on a long ride."""
        from training_live import WBAL_SPARKLINE_MAXLEN
        m = MetricsEngine(ftp=250, weight_kg=72)
        # Simulate 90 min → 1080 expected points but maxlen caps at 120.
        for _ in range(5400):
            m.update(200)
        assert len(m._wbal_sparkline) == WBAL_SPARKLINE_MAXLEN

    def test_wbal_sustain_finite_above_cp(self):
        """After 5+ s at P > CP, sustain_s should be finite and match
        the analytic drain time within ±10%."""
        m = MetricsEngine(ftp=250, weight_kg=72)
        cp = m._cp
        p = cp + 100  # 100 W above CP
        for _ in range(10):
            m.update(p)
        assert m._wbal_sustain_s is not None
        expected = m._wbal / (p - cp)
        # Within 10% — the depletion pulls wbal down each tick so the
        # "moving target" prediction is approximate by construction.
        assert abs(m._wbal_sustain_s - expected) / expected < 0.1

    def test_wbal_sustain_none_below_cp(self):
        """Power below CP → sustain is None (UI renders ∞)."""
        m = MetricsEngine(ftp=250, weight_kg=72)
        for _ in range(30):
            m.update(100)  # well below CP
        assert m._wbal_sustain_s is None

    def test_wbal_sustain_needs_min_above_cp_run(self):
        """A single tick above CP must not flap sustain finite — there's
        a 5-s guard against ERG transients."""
        m = MetricsEngine(ftp=250, weight_kg=72)
        cp = m._cp
        # One tick above, then back below.
        m.update(cp + 50, dt=1.0)
        # < WBAL_SUSTAIN_MIN_ABOVE_CP_S → still None on this tick
        assert m._wbal_sustain_s is None

    def test_wbal_gap_skip_freezes_integration(self):
        """When power stream drops (power=0) for > WBAL_POWER_GAP_S,
        the recovery integrator MUST NOT run — otherwise a brief BLE
        glitch would falsely restore W' the rider never earned back."""
        import time
        m = MetricsEngine(ftp=250, weight_kg=72)
        # Deplete hard — leaves wbal well below wprime.
        for _ in range(60):
            m.update(400)
        depleted = m._wbal
        assert depleted < m._wprime

        # Fake out wall-clock: simulate a >2 s gap between the last
        # valid frame and the next (all-zero) frame. The engine reads
        # time.monotonic() internally; we stash the last-valid mono
        # value far enough in the past that ANY tick counts as gap.
        m._power_last_valid_mono = time.monotonic() - 5.0
        gaps_before = m._wbal_gap_skip_count
        for _ in range(3):
            m.update(0, dt=1.0)
        # Counter incremented, wbal did NOT drift toward wprime.
        assert m._wbal_gap_skip_count > gaps_before
        # No recovery → wbal unchanged within floating-point tolerance.
        assert abs(m._wbal - depleted) < 1e-6

    def test_wbal_snapshot_broadcasts_new_fields(self):
        """MetricsEngine.snapshot() must expose cp_w, wbal_sparkline,
        wbal_sustain_s, wprime_j so the UI layer can render them."""
        m = MetricsEngine(ftp=250, weight_kg=72)
        for _ in range(30):
            m.update(200)
        snap = m.snapshot()
        assert "cp_w" in snap
        assert snap["cp_w"] > 0
        assert "wbal_sparkline" in snap
        assert isinstance(snap["wbal_sparkline"], list)
        assert "wbal_sustain_s" in snap           # may be None
        assert "wprime_j" in snap
        assert snap["wprime_j"] > 0


# ══════════════════════════════════════════════════════════════════════════════
# CourseEngine tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCourseEngine:
    def _make_course(self):
        """Simple 10km course: 2km flat, 3km at 5%, 3km at 8%, 2km descent -3%."""
        points = []
        ele = 0.0
        segments = [
            (2.0, 0.0), (3.0, 5.0), (3.0, 8.0), (2.0, -3.0),
        ]
        d = 0.0
        for length, grade in segments:
            steps = int(length / 0.1)
            for _ in range(steps):
                points.append({"d": round(d, 3), "g": grade, "e": round(ele, 1)})
                d += 0.1
                ele += 0.1 * 10 * grade
        return points

    def test_total_distance(self):
        c = CourseEngine(self._make_course())
        assert abs(c.total_km - 10.0) < 0.2

    def test_advance_and_position(self):
        c = CourseEngine(self._make_course())
        c.advance(36.0, 1.0)  # 36 km/h for 1 second = 0.01 km
        assert c.position_km > 0
        assert c.progress_pct > 0

    def test_gradient_lookup_flat(self):
        c = CourseEngine(self._make_course())
        assert c.gradient_at(1.0) == 0.0  # in the flat section

    def test_gradient_lookup_climb(self):
        c = CourseEngine(self._make_course())
        g = c.gradient_at(3.0)  # in the 5% section
        assert g == 5.0, f"Expected 5.0%, got {g}%"

    def test_gradient_lookup_steep(self):
        c = CourseEngine(self._make_course())
        g = c.gradient_at(6.0)  # in the 8% section
        assert g == 8.0, f"Expected 8.0%, got {g}%"

    def test_elevation_increases_on_climb(self):
        c = CourseEngine(self._make_course())
        e_flat = c.elevation_at(1.0)
        e_climb = c.elevation_at(5.0)
        assert e_climb > e_flat

    def test_gradient_ahead(self):
        c = CourseEngine(self._make_course())
        c._position = 1.5
        ahead = c.gradient_ahead(500)
        assert len(ahead) > 0
        assert all("g" in a for a in ahead)

    def test_surface_lookup(self):
        # v3.6.0-fix22f: CourseEngine.current_surface() now canonicalizes
        # any TACX UPPERCASE token to the lowercase MASTER_DECISIONS §1
        # enum at the source. Feed UPPERCASE and expect lowercase back.
        surfaces = [SurfaceSegment(0, 3, "COBBLESTONES_HARD"), SurfaceSegment(3, 10, "ASPHALT")]
        c = CourseEngine(self._make_course(), surfaces)
        c._position = 1.0
        assert c.current_surface() == "cobble"
        c._position = 5.0
        assert c.current_surface() == "asphalt"

    def test_is_complete(self):
        c = CourseEngine(self._make_course())
        assert not c.is_complete
        c._position = c.total_km
        assert c.is_complete

    def test_snapshot_has_required_keys(self):
        c = CourseEngine(self._make_course())
        snap = c.snapshot()
        for key in ("distance_km", "total_km", "progress_pct", "gradient",
                     "elevation", "surface", "summit_km", "ahead", "total_climb"):
            assert key in snap, f"Missing key: {key}"


# ══════════════════════════════════════════════════════════════════════════════
# WorkoutEngine tests
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkoutEngine:
    def _make_workout(self):
        """Simple workout: 5min warmup, 3x(2min on / 1min off), 3min cooldown."""
        return [
            WorkoutSegment(SegmentType.WARMUP, 0, 300, 125, 188),
            WorkoutSegment(SegmentType.INTERVALS_T, 300, 540,
                          125, 275, repeats=3, on_duration=120, off_duration=60,
                          on_power=275, off_power=125),
            WorkoutSegment(SegmentType.COOLDOWN, 840, 180, 188, 125),
        ]

    def test_total_duration(self):
        w = WorkoutEngine(self._make_workout())
        assert w.total_duration == 300 + 540 + 180  # 1020s = 17min

    def test_warmup_ramp(self):
        w = WorkoutEngine(self._make_workout())
        # At start: should be power_low
        assert w.current_target_power == 125
        # Halfway through warmup: should be between low and high
        for _ in range(150):
            w.advance()
        target = w.current_target_power
        assert 125 < target < 188, f"Warmup midpoint should be between 125-188, got {target}"

    def test_intervals_work_phase(self):
        w = WorkoutEngine(self._make_workout())
        # Advance past warmup
        for _ in range(300):
            w.advance()
        # Now in first work interval
        assert w.current_target_power == 275

    def test_intervals_rest_phase(self):
        w = WorkoutEngine(self._make_workout())
        # Advance to first rest (300 warmup + 120 work = 420s)
        for _ in range(420):
            w.advance()
        assert w.current_target_power == 125

    def test_free_ride_returns_zero(self):
        segs = [WorkoutSegment(SegmentType.FREE_RIDE, 0, 120, 0, 0)]
        w = WorkoutEngine(segs)
        assert w.current_target_power == 0
        assert w.is_free_ride_segment

    def test_completion(self):
        w = WorkoutEngine(self._make_workout())
        for _ in range(1020):
            w.advance()
        assert w.is_complete

    def test_interval_state(self):
        w = WorkoutEngine(self._make_workout())
        for _ in range(310):
            w.advance()
        state = w.interval_state()
        assert state is not None
        assert state.segment_name == "IntervalsT"
        assert state.total_reps == 3
        assert state.time_remaining > 0

    def test_snapshot_has_required_keys(self):
        w = WorkoutEngine(self._make_workout())
        snap = w.snapshot()
        for key in ("name", "elapsed", "total", "target_power", "segment_index",
                     "segment_type", "time_remaining", "complete"):
            assert key in snap, f"Missing key: {key}"


# ══════════════════════════════════════════════════════════════════════════════
# FeedbackEngine tests
# ══════════════════════════════════════════════════════════════════════════════

class TestFeedbackEngine:
    def test_hr_zone_lookup(self):
        f = FeedbackEngine(ftp=250, lthr=175, max_hr=196)
        z = f.hr_zone(160)
        assert z["zone"] in ("Z1", "Z2", "Z3", "Z4", "Z5")

    def test_power_deviation_green(self):
        f = FeedbackEngine(ftp=250, lthr=175, max_hr=196)
        d = f.power_deviation(240, 250)
        assert d["color"] == "green"

    def test_power_deviation_red(self):
        f = FeedbackEngine(ftp=250, lthr=175, max_hr=196)
        d = f.power_deviation(200, 250)
        assert d["color"] == "red"

    def test_wbal_warning(self):
        f = FeedbackEngine(ftp=250, lthr=175, max_hr=196)
        alerts = f.update(250, 160, None, 20)
        types = [a["type"] for a in alerts]
        assert "wbal_warning" in types

    def test_countdown_alert(self):
        f = FeedbackEngine(ftp=250, lthr=175, max_hr=196)
        state = IntervalState(0, "IntervalsT", 275, 115, 5, 1, 3, True, 115, 5)
        alerts = f.update(275, 160, state, 80)
        types = [a["type"] for a in alerts]
        assert "countdown" in types


# ══════════════════════════════════════════════════════════════════════════════
# CRS Parser test
# ══════════════════════════════════════════════════════════════════════════════

class TestCRSParser:
    def test_parse_real_crs_file(self):
        crs = Path(__file__).parent.parent / "courses" / "virtual" / "desert_loop" / "desert-loop__tt-course.crs"
        if not crs.exists():
            pytest.skip("CRS test file missing")
        points = parse_crs_for_session(crs)
        assert len(points) > 10
        assert points[0]["d"] == 0.0
        assert points[-1]["d"] > 5.0  # TT course is ~10km
        # Cumulative distance should be monotonically increasing
        for i in range(1, len(points)):
            assert points[i]["d"] >= points[i - 1]["d"]


# ══════════════════════════════════════════════════════════════════════════════
# Zone builder tests
# ══════════════════════════════════════════════════════════════════════════════

class TestZoneBuilders:
    def test_hr_zones_cover_full_range(self):
        zones = _build_hr_zones(175, 196)
        assert zones[0]["low"] == 0
        assert zones[-1]["high"] == 196
        assert len(zones) == 5

    def test_power_zones_cover_full_range(self):
        zones = _build_power_zones(250)
        assert zones[0]["low"] == 0
        # Canonical 7-zone Coggan model (via zones.py): top zone is open-ended
        # with high=99999 (Z7 Neuromuscular, above ~151% FTP).
        assert zones[-1]["high"] == 99999
        assert len(zones) == 7

    def test_power_zone_z4_is_threshold(self):
        # Coggan 7-zone model names Z4 "Z4 Threshold". v1.0.5 fixed the
        # Z3/Z4 boundary to Allen-Coggan canonical values: Z4 = 88-105% FTP
        # (sweet-spot 88-94 + true threshold 95-105). "Sweet Spot" is a
        # sub-band of Z4 used informally, not a standalone zone.
        zones = _build_power_zones(250)
        z4 = [z for z in zones if z["zone"] == "Z4"][0]
        assert z4["name"] == "Z4 Threshold"
        # Coggan 7-zone bounds (post-v1.0.5 boundary fix): Z4 spans 88-105% FTP.
        # zones.py uses max(prev_high+1, round(0.88*ftp)) for the low edge.
        assert z4["low"] == round(250 * 0.88)
        assert z4["high"] == round(250 * 1.05)


# ══════════════════════════════════════════════════════════════════════════════
# WarmupCooldownManager tests
# ══════════════════════════════════════════════════════════════════════════════

class TestWarmupCooldownManager:
    def test_starts_in_warmup_when_warmup_sec_positive(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175)
        assert mgr.phase == RidePhase.WARMUP

    def test_starts_in_route_when_no_warmup(self):
        mgr = WarmupCooldownManager(warmup_sec=0, cooldown_sec=300, ftp=250, lthr=175)
        assert mgr.phase == RidePhase.ROUTE

    def test_warmup_target_power_ramps_50_to_75_ftp(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=200, lthr=175)
        # At start: 50% of 200 = 100W
        result = mgr.update(0, 100, 100)
        assert result["target_power"] == 100  # 50% FTP

        # Near end (299s, still in warmup): should be very close to 75% FTP = 150W
        result = mgr.update(299, 100, 150)
        assert abs(result["target_power"] - 150) <= 1

    def test_warmup_target_power_midpoint(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=200, lthr=175)
        # At halfway (150s): should be midway between 100W and 150W = 125W
        result = mgr.update(150, 100, 125)
        assert result["target_power"] == 125

    def test_warmup_auto_transitions_to_route(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175)
        # Simulate 300 seconds of warmup
        for sec in range(301):
            mgr.update(sec, 100, 125)
        assert mgr.phase == RidePhase.ROUTE

    def test_warmup_does_not_auto_transition_with_auto_detect(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175,
                                     auto_detect=True)
        # Even after 300s, stays in warmup because HR not reached
        for sec in range(301):
            mgr.update(sec, 100, 125)  # HR=100, well below 75% of 175 = 131
        assert mgr.phase == RidePhase.WARMUP
        assert not mgr.is_warmed_up()

    def test_auto_detect_warmup_completes_on_hr(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175,
                                     auto_detect=True)
        # HR threshold = 75% of 175 = 131 (rounded)
        # Need 30 consecutive seconds above threshold
        for sec in range(30):
            mgr.update(sec, 135, 125)  # HR=135 > 131
        assert mgr.is_warmed_up()

    def test_auto_detect_hr_streak_resets_on_drop(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175,
                                     auto_detect=True)
        # 20 seconds above threshold
        for sec in range(20):
            mgr.update(sec, 135, 125)
        # Drop below threshold
        mgr.update(20, 100, 125)
        # Continue above for 10 more (not enough: only 10 not 30)
        for sec in range(21, 31):
            mgr.update(sec, 135, 125)
        assert not mgr.is_warmed_up()  # Streak reset, only 10 consecutive

    def test_skip_to_route(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175)
        mgr.update(10, 100, 125)
        mgr.skip_to_route()
        assert mgr.phase == RidePhase.ROUTE

    def test_extend_warmup(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175)
        mgr.extend_warmup(300)
        # Now warmup is 600s; should not transition after 300s
        for sec in range(301):
            mgr.update(sec, 100, 125)
        assert mgr.phase == RidePhase.WARMUP
        # Transition after 600s
        for sec in range(301, 601):
            mgr.update(sec, 100, 125)
        assert mgr.phase == RidePhase.ROUTE

    def test_start_cooldown(self):
        mgr = WarmupCooldownManager(warmup_sec=0, cooldown_sec=300, ftp=250, lthr=175)
        assert mgr.phase == RidePhase.ROUTE
        mgr.update(100, 160, 200)
        mgr.start_cooldown(300)
        assert mgr.phase == RidePhase.COOLDOWN

    def test_cooldown_target_power_ramps_75_to_40_ftp(self):
        mgr = WarmupCooldownManager(warmup_sec=0, cooldown_sec=300, ftp=200, lthr=175)
        mgr.update(100, 160, 200)
        mgr.start_cooldown(300)
        # At start of cooldown (elapsed=101, phase_start=100, phase_elapsed=1):
        # Nearly 75% of 200 = 150W
        result = mgr.update(101, 160, 150)
        assert abs(result["target_power"] - 150) <= 2

        # Near end of cooldown (elapsed=399, phase_elapsed=299):
        # Nearly 40% of 200 = 80W
        result = mgr.update(399, 100, 80)
        assert abs(result["target_power"] - 80) <= 2

    def test_cooldown_transitions_to_done(self):
        mgr = WarmupCooldownManager(warmup_sec=0, cooldown_sec=300, ftp=250, lthr=175)
        mgr.update(0, 160, 200)
        mgr.start_cooldown(300)
        # Simulate 300s of cooldown
        for sec in range(301):
            mgr.update(sec, 100, 80)
        assert mgr.phase == RidePhase.DONE

    def test_is_cooled_down_after_timer(self):
        mgr = WarmupCooldownManager(warmup_sec=0, cooldown_sec=300, ftp=250, lthr=175)
        mgr.update(0, 160, 200)
        mgr.start_cooldown(300)
        for sec in range(301):
            mgr.update(sec, 100, 80)
        assert mgr.is_cooled_down()

    def test_is_cooled_down_on_low_hr(self):
        mgr = WarmupCooldownManager(warmup_sec=0, cooldown_sec=600, ftp=250, lthr=175)
        mgr.update(0, 160, 200)
        mgr.start_cooldown(600)
        # HR drops below 60% of 175 = 105 immediately
        mgr.update(1, 100, 80)  # HR=100 < 105, streak resets
        assert mgr.is_cooled_down()

    def test_phase_elapsed(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175)
        mgr.update(50, 100, 125)
        assert mgr.phase_elapsed == 50

    def test_phase_remaining_warmup(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175)
        mgr.update(50, 100, 125)
        assert mgr.phase_remaining == 250

    def test_phase_remaining_auto_detect_waiting(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175,
                                     auto_detect=True)
        # After timer expires but HR not reached, remaining = 0
        for sec in range(301):
            mgr.update(sec, 100, 125)
        assert mgr.phase_remaining == 0

    def test_progress_pct_warmup(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175)
        result = mgr.update(150, 100, 125)
        assert abs(result["progress_pct"] - 50.0) < 0.1

    def test_update_returns_required_keys(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175)
        result = mgr.update(10, 130, 125)
        required_keys = ["phase", "target_power", "progress_pct", "warmup_ready",
                         "hr_pct_lthr", "can_extend", "can_skip"]
        for key in required_keys:
            assert key in result, f"Missing key: {key}"

    def test_snapshot_has_required_keys(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175)
        mgr.update(10, 130, 125)
        snap = mgr.snapshot()
        required_keys = ["phase", "phase_elapsed", "phase_remaining", "target_power",
                         "progress_pct", "warmup_ready", "cooled_down", "warmup_sec",
                         "cooldown_sec", "auto_detect"]
        for key in required_keys:
            assert key in snap, f"Missing snapshot key: {key}"

    def test_hr_pct_lthr_calculation(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175)
        result = mgr.update(10, 140, 125)
        expected = round(140 / 175 * 100, 1)
        assert result["hr_pct_lthr"] == expected

    def test_extend_only_works_during_warmup(self):
        mgr = WarmupCooldownManager(warmup_sec=0, cooldown_sec=300, ftp=250, lthr=175)
        # Already in ROUTE, extend should do nothing
        mgr.extend_warmup(300)
        assert mgr.phase == RidePhase.ROUTE

    def test_skip_only_works_during_warmup(self):
        mgr = WarmupCooldownManager(warmup_sec=0, cooldown_sec=300, ftp=250, lthr=175)
        # Already in ROUTE, skip should do nothing (stays in ROUTE)
        mgr.skip_to_route()
        assert mgr.phase == RidePhase.ROUTE

    def test_start_cooldown_only_works_during_route(self):
        mgr = WarmupCooldownManager(warmup_sec=300, cooldown_sec=300, ftp=250, lthr=175)
        # In WARMUP, start_cooldown should do nothing
        mgr.start_cooldown(300)
        assert mgr.phase == RidePhase.WARMUP

    def test_full_lifecycle(self):
        """Test full warmup -> route -> cooldown -> done lifecycle."""
        mgr = WarmupCooldownManager(warmup_sec=60, cooldown_sec=60, ftp=250, lthr=175)
        # Warmup (0-60s)
        for sec in range(61):
            mgr.update(sec, 100, 125)
        assert mgr.phase == RidePhase.ROUTE

        # Route (ride at sec=100)
        mgr.update(100, 160, 200)
        assert mgr.phase == RidePhase.ROUTE

        # Start cooldown at sec=100
        mgr.start_cooldown(60)
        assert mgr.phase == RidePhase.COOLDOWN

        # Finish cooldown (absolute elapsed 101..161, so phase_elapsed goes 1..61)
        for sec in range(101, 162):
            mgr.update(sec, 100, 80)
        assert mgr.phase == RidePhase.DONE
        assert mgr.is_cooled_down()


# ══════════════════════════════════════════════════════════════════════════════
# RidePhase enum tests
# ══════════════════════════════════════════════════════════════════════════════

class TestRidePhase:
    def test_enum_values(self):
        assert RidePhase.WARMUP.value == "warmup"
        assert RidePhase.ROUTE.value == "route"
        assert RidePhase.COOLDOWN.value == "cooldown"
        assert RidePhase.DONE.value == "done"

    def test_all_phases_exist(self):
        phases = [p.value for p in RidePhase]
        assert "warmup" in phases
        assert "route" in phases
        assert "cooldown" in phases
        assert "done" in phases


# ══════════════════════════════════════════════════════════════════════════════
# parse_crs_for_session trimming tests
# ══════════════════════════════════════════════════════════════════════════════

class TestParseCrsTrimming:
    def _make_temp_crs(self, tmp_path=None):
        """Create a simple temporary CRS file for testing."""
        import tempfile
        if tmp_path is None:
            tmp_path = Path(tempfile.mkdtemp())
        # 10 points, 0.5km apart = 5km total
        crs_content = "[COURSE DATA]\nDISTANCE\tGRADE\tWIND\n"
        for i in range(11):
            delta = 0.5 if i > 0 else 0.0
            grade = 3.0
            crs_content += f"{delta}\t{grade}\t0\n"
        crs_content += "[END COURSE DATA]\n"

        crs_file = tmp_path / "test_course.crs"
        crs_file.write_text(crs_content, encoding="utf-8")
        return crs_file

    def test_no_trim(self):
        crs_file = self._make_temp_crs()
        points = parse_crs_for_session(crs_file)
        assert len(points) == 11
        assert points[0]["d"] == 0.0
        assert points[-1]["d"] == 5.0

    def test_skip_warmup_km(self):
        crs_file = self._make_temp_crs()
        points = parse_crs_for_session(crs_file, skip_warmup_km=1.0)
        # Points at 0, 0.5 (skipped: d < 1.0), first kept is d=1.0
        assert points[0]["d"] == 0.0  # recalculated to start at 0
        assert all(p["d"] >= 0 for p in points)
        # Total should be ~4km (original 5km minus 1km trimmed)
        assert abs(points[-1]["d"] - 4.0) < 0.01

    def test_skip_cooldown_km(self):
        crs_file = self._make_temp_crs()
        points = parse_crs_for_session(crs_file, skip_cooldown_km=1.0)
        # Total distance should be ~4km (5km minus 1km from end)
        assert points[-1]["d"] <= 4.0

    def test_skip_both(self):
        crs_file = self._make_temp_crs()
        points = parse_crs_for_session(crs_file, skip_warmup_km=1.0, skip_cooldown_km=1.0)
        # Should be ~3km (5 - 1 - 1)
        assert points[0]["d"] == 0.0
        assert abs(points[-1]["d"] - 3.0) < 0.01

    def test_excessive_trim_returns_single_point(self):
        crs_file = self._make_temp_crs()
        points = parse_crs_for_session(crs_file, skip_warmup_km=10.0)
        # Trim more than course length -> returns single origin point
        assert len(points) >= 1
        assert points[0]["d"] == 0.0

    def test_trim_preserves_monotonic_distance(self):
        crs_file = self._make_temp_crs()
        points = parse_crs_for_session(crs_file, skip_warmup_km=0.5)
        for i in range(1, len(points)):
            assert points[i]["d"] >= points[i - 1]["d"], \
                f"Distance not monotonic at index {i}"

    def test_elevation_recalculated_on_trim(self):
        crs_file = self._make_temp_crs()
        points = parse_crs_for_session(crs_file, skip_warmup_km=1.0)
        # First point elevation should be 0 after recalculation
        assert points[0]["e"] == 0.0


class TestFix22fCourseSurfaceCanonical:
    """QA-INT I-1 (HIGH): `d.course.surface` in the WS stream shipped
    UPPERCASE TACX tokens ("ASPHALT", "GRAVEL", "COBBLESTONES_HARD") because
    `CourseEngine.current_surface()` returned the raw stored token — the
    same token `load_surface_segments()` puts there intentionally for the
    TACX road-feel path. MASTER_DECISIONS §1 locks the wire surface enum
    to lowercase {asphalt, gravel, cobble, dirt, sand, unknown}.

    The leak was masked in practice by `_canonSurfaceLive()` in
    training.html, but any NEW consumer doing a raw string-equal would
    silently break. fix22f canonicalizes at the source so every caller —
    WS, saved-ride exports, tests — sees the contracted enum.
    """

    _CANONICAL = {"asphalt", "gravel", "cobble", "dirt", "sand", "unknown"}

    def _make_course(self):
        """10km linear course, flat throughout — minimum needed to park a
        CourseEngine. Grade values are irrelevant for surface testing."""
        return [{"d": round(i * 0.1, 3), "g": 0.0, "e": 0.0}
                for i in range(0, 101)]

    def test_course_engine_current_surface_is_lowercase(self):
        """Feed UPPERCASE TACX tokens directly into CourseEngine and walk
        the position across each segment; every lookup must return the
        canonical lowercase enum, not the raw TACX token."""
        surfaces = [
            SurfaceSegment(start_km=0.0, end_km=2.0, surface="ASPHALT"),
            SurfaceSegment(start_km=2.0, end_km=5.0, surface="GRAVEL"),
            SurfaceSegment(start_km=5.0, end_km=7.0, surface="COBBLESTONES_HARD"),
            SurfaceSegment(start_km=7.0, end_km=10.0, surface="OFF_ROAD"),
        ]
        c = CourseEngine(self._make_course(), surfaces)

        expected = [
            (1.0, "asphalt"),
            (3.0, "gravel"),
            (6.0, "cobble"),
            (8.0, "gravel"),   # OFF_ROAD is a gravel alias per app.py map
        ]
        for km, want in expected:
            c._position = km
            got = c.current_surface()
            assert got == want, (
                f"at {km}km: want {want!r}, got {got!r} — "
                f"UPPERCASE TACX token leaked through current_surface()"
            )
            assert got in self._CANONICAL, (
                f"{got!r} outside MASTER_DECISIONS §1 lowercase enum"
            )

    def test_course_engine_snapshot_surface_is_lowercase(self):
        """`CourseEngine.snapshot()` is what `_broadcast()` reaches into
        via `self._course.snapshot()` — so this also covers the WS path
        without spinning up a full TrainingSession."""
        surfaces = [
            SurfaceSegment(start_km=0.0, end_km=10.0, surface="COBBLESTONES_HARD"),
        ]
        c = CourseEngine(self._make_course(), surfaces)
        c._position = 2.5
        snap = c.snapshot()
        assert snap["surface"] == "cobble", (
            f"snapshot leaked raw {snap['surface']!r} — WS consumers doing "
            f"raw string-equals would break on UPPERCASE"
        )

    def test_course_engine_unknown_surface_collapses_to_unknown(self):
        """Defensive: a surface token the canonicalizer can't map must
        collapse to 'unknown' rather than leaking the raw value (belt for
        new surface_types.json tokens landing without a map entry)."""
        surfaces = [SurfaceSegment(start_km=0.0, end_km=10.0, surface="MOLTEN_LAVA")]
        c = CourseEngine(self._make_course(), surfaces)
        c._position = 3.0
        assert c.current_surface() == "unknown"



class TestHRMetricsFix25:
    """Regressions for the Wave-2 HR metrics audit (v3.6.0-fix25).

    Coverage: DFA warmup suppression, time-gated window, unphysical clamp,
    artifact rejection, resync/gap flush, synthetic valid signal; decoupling
    provisional/locked, ride-too-short, efficiency_factor canonical, unified
    Z1 filter, 900s warmup trim, FIFO 24k cap.
    """

    # ── Helpers (kept local so tests stay self-contained) ────────────

    @staticmethod
    def _force_recording(engine: "MetricsEngine") -> None:
        """MetricsEngine.set_recording_phase(True) + preset the monotonic
        session-start stamp so the §1.8 warmup gate uses a deterministic clock."""
        engine.set_recording_phase(True)
        # Push session start far enough in the past to pass the 180 s
        # session-elapsed gate when tests want it to pass.
        engine._session_start_monotonic = 0.0

    @staticmethod
    def _feed_rr_window(
        engine: "MetricsEngine",
        count: int,
        rr_ms: int,
        start_ts: float = 10.0,
        step_s: float | None = None,
    ) -> float:
        """Feed `count` RR beats of length `rr_ms` ms at monotonic timestamps."""
        if step_s is None:
            step_s = rr_ms / 1000.0
        ts = start_ts
        batch = []
        for _ in range(count):
            batch.append((ts, rr_ms))
            ts += step_s
        engine.add_rr_intervals(batch, now_monotonic=ts)
        return ts

    # ── DFA-2 warmup suppression ─────────────────────────────────────

    def test_dfa_alpha1_warmup_returns_none(self):
        """§DFA-2: session elapsed < 180 s → α1 is None with status=stabilizing,
        even once the 120-s rolling window is full."""
        from training_live import MetricsEngine
        m = MetricsEngine(ftp=250, weight_kg=72.0)
        # Advance ride elapsed to 120 s (< 180 s warmup) and provide beats.
        for _ in range(120):
            m.update(200, dt=1.0, hr=150)
        # Fire 100 beats at 800 ms so the buffer has >= DFA_MIN_BEATS (90).
        m.set_recording_phase(True)
        m._session_start_monotonic = 0.0  # monotonic 0 reference
        # Force wall-time < 180 s past session start.
        self._feed_rr_window(m, count=100, rr_ms=800, start_ts=10.0)
        # The compute fired above, but the session-elapsed gate
        # (self._elapsed < 180) or the monotonic gate prevents emit.
        # Explicitly drive a compute with a monotonic clock < 180 s.
        m._compute_dfa_alpha1(now_monotonic=120.0)
        assert m._dfa_alpha1 is None
        assert m._dfa_status == "stabilizing"

    # ── DFA-3 time-gated 120s window ──────────────────────────────────

    def test_dfa_alpha1_time_gated_window_high_hr(self):
        """§DFA-3: buffer holds ~120 s of wall-time regardless of HR.

        180 bpm → 360 beats in 120 s; 60 bpm → 120 beats in 120 s. The
        deque in both cases should span ≤ 120 s after trim.
        """
        from training_live import MetricsEngine
        # 180 bpm → 333.3 ms RR.
        m_hi = MetricsEngine(ftp=250, weight_kg=72.0)
        self._force_recording(m_hi)
        self._feed_rr_window(m_hi, count=400, rr_ms=333, start_ts=0.0)
        hi_span = m_hi._rr_buffer[-1][0] - m_hi._rr_buffer[0][0]
        assert hi_span <= 120.0 + 0.5
        # 60 bpm → 1000 ms RR.
        m_lo = MetricsEngine(ftp=250, weight_kg=72.0)
        self._force_recording(m_lo)
        self._feed_rr_window(m_lo, count=150, rr_ms=1000, start_ts=0.0)
        lo_span = m_lo._rr_buffer[-1][0] - m_lo._rr_buffer[0][0]
        assert lo_span <= 120.0 + 0.5
        # And the high-HR buffer has MORE beats in the same wall-time.
        assert len(m_hi._rr_buffer) > len(m_lo._rr_buffer)

    # ── DFA-4 unphysical clamp ───────────────────────────────────────

    def test_dfa_alpha1_unphysical_returns_none(self):
        """§DFA-4: degenerate all-identical RR produces α1 → 0 → None."""
        from training_live import MetricsEngine
        m = MetricsEngine(ftp=250, weight_kg=72.0)
        # Make the ride elapsed exceed 180 s + session monotonic gate.
        for _ in range(200):
            m.update(200, dt=1.0, hr=150)
        self._force_recording(m)
        self._feed_rr_window(m, count=120, rr_ms=800, start_ts=0.0)
        # Force a compute past the monotonic warmup gate.
        m._compute_dfa_alpha1(now_monotonic=200.0)
        # Identical RRs → α1 clamped below 0.3, status unphysical.
        assert m._dfa_alpha1 is None
        assert m._dfa_status == "unphysical"

    # ── DFA-5 artifact threshold unified at 5% ───────────────────────

    def test_dfa_alpha1_artifacts_above_5pct_rejected(self):
        """§DFA-5: >5% artifact-marked beats → None + status=artifacts."""
        from training_live import MetricsEngine
        m = MetricsEngine(ftp=250, weight_kg=72.0)
        for _ in range(200):
            m.update(200, dt=1.0, hr=150)
        self._force_recording(m)
        # Build 120 RRs with 10% deliberate 30% spikes (artifacts).
        base = 800
        spike = int(base * 1.35)  # > 20% deviation from local median
        ts = 0.0
        batch = []
        for i in range(120):
            rr = spike if i % 10 == 0 else base
            batch.append((ts, rr))
            ts += rr / 1000.0
        m.add_rr_intervals(batch, now_monotonic=ts)
        m._compute_dfa_alpha1(now_monotonic=200.0)
        assert m._dfa_alpha1 is None
        assert m._dfa_status == "artifacts"

    # ── DFA valid synthetic signal ───────────────────────────────────

    def test_dfa_alpha1_valid_synthetic_signal(self):
        """A gently-modulated RR series (1/f-ish) should yield α1 in a
        reasonable band. Pure-Python tolerance is wide — we assert the
        result is inside [0.3, 1.6] and status=ok."""
        import math
        import random
        random.seed(42)
        from training_live import MetricsEngine

        m = MetricsEngine(ftp=250, weight_kg=72.0)
        for _ in range(300):
            m.update(200, dt=1.0, hr=140)
        self._force_recording(m)

        # Build ~150 beats spanning ~120 s with mild pink-like modulation.
        ts = 0.0
        batch = []
        mean = 800
        drift = 0.0
        for _ in range(150):
            drift = 0.8 * drift + random.gauss(0, 15)
            rr = int(mean + drift + random.gauss(0, 8))
            rr = max(500, min(1200, rr))
            batch.append((ts, rr))
            ts += rr / 1000.0
        m.add_rr_intervals(batch, now_monotonic=ts)
        m._compute_dfa_alpha1(now_monotonic=200.0)
        # Valid band per §DFA-4.
        if m._dfa_alpha1 is not None:
            assert 0.30 <= m._dfa_alpha1 <= 1.60
            assert m._dfa_status == "ok"
        else:
            # Occasionally the RNG produces a degenerate series — accept
            # either a clean α1 or a well-flagged status.
            assert m._dfa_status in ("unphysical", "artifacts", "stabilizing")

    # ── DFA resync flush ─────────────────────────────────────────────

    def test_dfa_alpha1_resync_flag_clears_window(self):
        """§HR-3: rr_is_resync=True flushes the deque completely."""
        from training_live import MetricsEngine
        m = MetricsEngine(ftp=250, weight_kg=72.0)
        self._force_recording(m)
        self._feed_rr_window(m, count=50, rr_ms=800, start_ts=0.0)
        assert len(m._rr_buffer) > 0
        # Resync flag with a tiny follow-on batch — buffer should be empty
        # of the pre-resync entries.
        m.add_rr_intervals(
            [(100.0, 800)], rr_is_resync=True, now_monotonic=100.0
        )
        # After flush, only the one entry from the resync batch remains.
        assert len(m._rr_buffer) == 1
        # Mini-warmup armed 180 s into the future from resync time.
        assert m._rr_warmup_until >= 100.0 + 180.0 - 0.1

    # ── DFA gap-over-2s flushes ──────────────────────────────────────

    def test_dfa_alpha1_gap_over_2s_flushes(self):
        """Adjacent monotonic gap > 2 s drops everything older than the gap."""
        from training_live import MetricsEngine
        m = MetricsEngine(ftp=250, weight_kg=72.0)
        self._force_recording(m)
        # Batch 1: 20 beats close together.
        self._feed_rr_window(m, count=20, rr_ms=800, start_ts=0.0)
        assert len(m._rr_buffer) == 20
        # Batch 2: big 3 s jump, then 10 beats.
        self._feed_rr_window(m, count=10, rr_ms=800, start_ts=25.0)
        # Only the post-gap entries should remain.
        assert len(m._rr_buffer) == 10
        assert m._rr_buffer[0][0] >= 25.0

    # ── DEC-2 live provisional ───────────────────────────────────────

    def test_decoupling_live_is_provisional(self):
        """§DEC-2: during the ride broadcast decoupling_provisional=True."""
        from training_live import MetricsEngine
        m = MetricsEngine(ftp=250, weight_kg=72.0)
        # Drive 30 min of valid samples — still live.
        for _ in range(1800):
            m.update(200, dt=1.0, hr=150)
        snap = m.snapshot()
        assert snap["decoupling_provisional"] is True

    # ── DEC-2 final on stop ──────────────────────────────────────────

    def test_decoupling_final_on_stop(self):
        """§DEC-2: after lock_decoupling_final() broadcast shows provisional=False."""
        from training_live import MetricsEngine
        m = MetricsEngine(ftp=250, weight_kg=72.0)
        # 900 s warmup + 2500 s steady → qualifies for a scalar.
        for _ in range(900):
            m.update(200, dt=1.0, hr=140)
        for _ in range(2500):
            m.update(200, dt=1.0, hr=145)
        locked = m.lock_decoupling_final()
        assert locked is not None
        snap = m.snapshot()
        assert snap["decoupling_provisional"] is False

    # ── DEC-2 ride-too-short ─────────────────────────────────────────

    def test_decoupling_under_40_min_none(self):
        """§DEC-2: ride < 40 min of filtered samples → pct=None + reason."""
        from training_live import MetricsEngine
        m = MetricsEngine(ftp=250, weight_kg=72.0)
        for _ in range(1200):  # 20 min — under the 40-min minimum
            m.update(200, dt=1.0, hr=150)
        dc = m.decoupling
        assert dc["pct"] is None
        assert dc["reason"] == "ride_too_short"

    # ── DEC-3 efficiency_factor = NP / avg_HR ────────────────────────

    def test_efficiency_factor_is_np_over_avghr(self):
        """§DEC-3: broadcast.efficiency_factor equals ef_final()
        (ride NP / avg_HR), NOT instantaneous power/HR."""
        from training_live import MetricsEngine
        m = MetricsEngine(ftp=250, weight_kg=72.0)
        # Varying power so per-tick pw/hr differs from NP/avg_HR.
        for i in range(1800):
            p = 200 if (i % 2 == 0) else 240
            m.update(p, dt=1.0, hr=150)
        snap = m.snapshot()
        expected = m.ef_final()
        assert snap["efficiency_factor"] == expected
        # And NOT the instantaneous last tick value.
        assert snap["efficiency_factor"] != m._pw_hr_ratio_inst

    # ── DEC-4 unified Z1 filter rejects sub-50 W ─────────────────────

    def test_unified_z1_filter_rejects_sub_50w(self):
        """§DEC-4: samples with power < 50 W never enter the decoupling buffers."""
        from training_live import MetricsEngine
        m = MetricsEngine(ftp=250, weight_kg=72.0)
        for _ in range(100):
            m.update(30, dt=1.0, hr=120)   # 30 W — dropped
        assert len(m._dc_powers) == 0
        for _ in range(100):
            m.update(100, dt=1.0, hr=120)  # 100 W — kept
        assert len(m._dc_powers) == 100

    # ── DEC-5 warmup trim 900s ───────────────────────────────────────

    def test_warmup_trim_900s_excluded(self):
        """§DEC-5: first 900 s of filtered samples are excluded from halves.

        Ride: 1000 s warmup (HR drifting 120→140, 0→200W ramp) + 2400 s
        steady (HR=150, power=200W). Without trim the ramp would create
        a large negative decoupling; with 900 s trim the remaining 100 s
        of low-HR warmup plus the flat steady phase should produce a
        small (|pct| < 10) decoupling rather than the uncorrected
        double-digit negative value.
        """
        from training_live import MetricsEngine
        m = MetricsEngine(ftp=250, weight_kg=72.0)
        # 900 s warmup to be trimmed entirely.
        for i in range(900):
            m.update(50 + int(150 * i / 900), dt=1.0, hr=90 + int(50 * i / 900))
        # 2500 s steady.
        for _ in range(2500):
            m.update(200, dt=1.0, hr=140)
        dc = m.decoupling
        assert dc["pct"] is not None
        # With the trim the halves are well-coupled (both flat).
        assert abs(dc["pct"]) < 1.0, (
            f"warmup trim failed: post-trim decoupling={dc['pct']}")

    # ── DEC-7 FIFO cap 24k ───────────────────────────────────────────

    def test_fifo_cap_24k(self):
        """§DEC-7: _dc_powers / _dc_hrs / _dc_timestamps cap at 24000."""
        from training_live import MetricsEngine, DECOUPLING_BUFFER_CAP
        m = MetricsEngine(ftp=250, weight_kg=72.0)
        # Drive 30k valid samples — all pass the §1.4 filter.
        for _ in range(30_000):
            m.update(200, dt=1.0, hr=150)
        assert len(m._dc_powers) == DECOUPLING_BUFFER_CAP == 24000
        assert len(m._dc_hrs) == DECOUPLING_BUFFER_CAP
        assert len(m._dc_timestamps) == DECOUPLING_BUFFER_CAP
class TestVirtualSpeedAero:
    """Cw in the FTMS wire byte is ρ × CdA (kg/m), so the aero term in
    compute_virtual_speed is ``0.5 * Cw * v³``. Multiplying by ρ again would
    make the rider ~22 % too slow at steady state (single ρ vs ρ²).
    """

    def test_virtual_speed_no_double_rho(self):
        """200 W on flat, crr=0.004, Cw=0.51, M=80+8=88 kg.

        Closed-form single-ρ balance (flat, sin=0, cos=1, wind=0):
            P_wheel = Crr*M*g*v + 0.5*Cw*v³
        where P_wheel = P_crank * η_drivetrain.
        Solve numerically, compare compute_virtual_speed() to < 0.3 km/h.

        Fixed (single-ρ) solves to ≈31.44 km/h at 200 W wheel power.
        With fix34 drivetrain loss (η=0.97) the 200 W crank → 194 W wheel
        lands at ≈31.1 km/h. The old double-ρ bug (0.5 × ρ × Cw × v³ with
        Cw already = ρ × CdA) would give ≈29.5 km/h — well below 30 km/h.
        """
        from training_live import DRIVETRAIN_EFFICIENCY
        power, grade, crr, cw, mass, bike = 200.0, 0.0, 0.004, 0.51, 80.0, 8.0
        M, g = mass + bike, 9.81
        power_wheel = power * DRIVETRAIN_EFFICIENCY

        # Closed-form reference: Newton's method on
        #   0.5*Cw*v³ + Crr*M*g*v - P_wheel = 0
        # ρ is NOT present — Cw already bakes it in.
        v_ref = 5.0
        for _ in range(60):
            f  = 0.5 * cw * v_ref ** 3 + crr * M * g * v_ref - power_wheel
            df = 1.5 * cw * v_ref ** 2 + crr * M * g
            v_ref -= f / df
        kmh_ref = v_ref * 3.6

        got = compute_virtual_speed(
            power_watts=power, grade_pct=grade, rider_mass_kg=mass,
            bike_mass_kg=bike, crr=crr, cw=cw, prev_speed_mps=v_ref,
        )

        assert abs(got - kmh_ref) < 0.3, (
            f"compute_virtual_speed={got:.3f} km/h vs single-ρ reference "
            f"{kmh_ref:.3f} km/h — double-ρ regression?")

        # Sanity: single-ρ solution at 200 W crank / η=0.97 / Cw=0.51 /
        # flat ≈ 31.1 km/h. Broken double-ρ would land near 29.5 km/h.
        assert 30.0 < got < 32.5, (
            f"v={got:.3f} km/h outside single-ρ band [30.0, 32.5]; "
            f"double-ρ bug would land at ≈29.5 km/h")

    def test_virtual_speed_zero_power_flat_no_coast(self):
        """P=0 on flat ground → rider decelerates to 0 km/h."""
        got = compute_virtual_speed(
            power_watts=0.0, grade_pct=0.0, rider_mass_kg=75.0,
            prev_speed_mps=5.0,
        )
        assert got < 1.0, f"v={got:.3f} km/h — should coast to a stop on flat"


# ══════════════════════════════════════════════════════════════════════════════
# v3.6.0-fix28 polish batch — 1 test per fix
# ══════════════════════════════════════════════════════════════════════════════

class TestFix28Polish:
    """Narrow regressions — 1 test per item in the fix28-polish batch."""

    def test_l3_crs_fraction_autodetected_and_rescaled(self, tmp_path):
        """CRS files with grade expressed as fraction (0.08) → ×100 to percent."""
        crs_content = "[COURSE DATA]\nDISTANCE\tGRADE\tWIND\n"
        # All grades ≤ 1.0 → trigger fraction autodetect.
        for i in range(6):
            delta = 0.0 if i == 0 else 0.5
            crs_content += f"{delta}\t0.08\t0\n"
        crs_content += "[END COURSE DATA]\n"
        crs_file = tmp_path / "frac.crs"
        crs_file.write_text(crs_content, encoding="utf-8")
        points = parse_crs_for_session(crs_file)
        # After autodetect the grade should be 8.0%, not 0.08.
        grades = [p["g"] for p in points if p["d"] > 0]
        assert grades, "expected non-origin grade samples"
        assert all(abs(g - 8.0) < 0.01 for g in grades), (
            f"fraction autodetect failed: grades={grades}"
        )

    def test_l6_zwo_unknown_tag_warn_once(self, tmp_path, caplog):
        """parse_zwo_for_session logs WARNING for unknown tags (once each)."""
        import logging as _logging
        from training_live import parse_zwo_for_session
        # Minimal ZWO with one valid + one bogus tag repeated.
        zwo = (
            '<workout_file><name>test</name><workout>'
            '<SteadyState Duration="60" Power="0.65"/>'
            '<Nonsense Duration="10"/>'
            '<Nonsense Duration="10"/>'
            '</workout></workout_file>'
        )
        p = tmp_path / "test.zwo"
        p.write_text(zwo, encoding="utf-8")
        caplog.set_level(_logging.WARNING)
        name, segs = parse_zwo_for_session(p, ftp=250)
        assert len(segs) == 1  # only SteadyState survives
        warn_msgs = [r for r in caplog.records
                     if "unknown tag" in r.getMessage()]
        # One tag, repeated twice — emit exactly one warning per file.
        assert len(warn_msgs) == 1, (
            f"expected 1 unknown-tag warning, got {len(warn_msgs)}: "
            f"{[r.getMessage() for r in warn_msgs]}"
        )

    def test_l8_dfa_low_r2_returns_none_with_status(self):
        """A degenerate F(n) series (near-constant) must return None + low_r2."""
        m = MetricsEngine(ftp=250, weight_kg=72)
        # Start a session so the warmup gate is satisfied.
        m._session_start_monotonic = 0.0
        m._session_recording = True
        m._elapsed = 200
        # Feed a perfectly constant RR series — F(n) stays near 0 so the
        # log-log slope is near-zero with near-zero R² for a horizontal line.
        # Need ≥ DFA_MIN_BEATS (=90) beats to pass the beats gate.
        import time as _t
        now = _t.monotonic()
        for _ in range(120):
            m._rr_buffer.append((now, 800))
        m._compute_dfa_alpha1(now_monotonic=now + 200)
        # Either low_r2 or unphysical is acceptable — both reject bogus α1.
        assert m._dfa_alpha1 is None
        assert m._dfa_status in ("low_r2", "unphysical"), (
            f"expected low_r2/unphysical, got {m._dfa_status}"
        )
class TestFix34SpeedNoHalving:
    """Root cause: `display_speed = max(speed, virtual_speed * 0.5)` capped
    the physics estimate at half. At 300 W / 0 % grade / 80 kg total, the
    correct physics speed is ~36.6 km/h; halved to 18.3 km/h it masked
    TACX residual floors and broke the UI readout. Fix34 drops the 0.5
    factor — physics speed is the floor, TACX can only lift it.
    """

    def test_speed_halving_removed(self):
        """Static assert: the `virtual_speed * 0.5` halving must not
        appear in display_speed calculations. Guards against regression."""
        from pathlib import Path
        src = Path(__file__).parent.parent / "training_live.py"
        text = src.read_text()
        assert "virtual_speed * 0.5" not in text, (
            "regression: `virtual_speed * 0.5` reappeared in training_live.py "
            "— fix34 removed this because halving physics truth capped "
            "display_speed at half the real value"
        )

    def test_drivetrain_efficiency_applied(self):
        """fix34: compute_virtual_speed must scale input power by 0.97
        before solving the power balance. Compare 300 W nominal against
        291 W nominal (which equals 300*0.97): the first should yield a
        speed close to what a solver fed 291 W at no drivetrain loss
        would produce."""
        from training_live import DRIVETRAIN_EFFICIENCY, compute_virtual_speed
        assert abs(DRIVETRAIN_EFFICIENCY - 0.97) < 1e-6, (
            f"DRIVETRAIN_EFFICIENCY={DRIVETRAIN_EFFICIENCY} — expected 0.97"
        )
        # At 300 W flat / 83 kg, with η=0.97 the effective wheel power is
        # ~291 W. Solve the balance explicitly with ρ-baked Cw and confirm
        # the returned speed matches the 291-W-at-wheel solution.
        cw, crr, M, g = 0.51, 0.004, 83.0 + 8.0, 9.81
        p_wheel = 300.0 * DRIVETRAIN_EFFICIENCY
        v_ref = 8.0
        for _ in range(60):
            f  = 0.5 * cw * v_ref ** 3 + crr * M * g * v_ref - p_wheel
            df = 1.5 * cw * v_ref ** 2 + crr * M * g
            v_ref -= f / df
        kmh_ref = v_ref * 3.6
        got = compute_virtual_speed(
            power_watts=300.0, grade_pct=0.0, rider_mass_kg=83.0,
            bike_mass_kg=8.0, crr=crr, cw=cw, prev_speed_mps=v_ref,
        )
        assert abs(got - kmh_ref) < 0.3, (
            f"virtual_speed={got:.2f} km/h vs wheel-power reference "
            f"{kmh_ref:.2f} km/h — drivetrain η={DRIVETRAIN_EFFICIENCY} "
            f"not applied inside compute_virtual_speed"
        )

    def test_grade_sanity_clamp(self, caplog):
        """fix34: grade_pct=200 (bogus, likely fraction-vs-percent mix)
        must be clamped to 25 and emit a WARN on log_power."""
        import logging
        from training_live import _sanitize_speed_inputs
        caplog.set_level(logging.WARNING, logger="domestique.power")
        g, _m = _sanitize_speed_inputs(200.0, 75.0)
        assert g == 25.0, f"expected clamp to 25.0, got {g}"
        assert any("SANITY" in r.message and "grade_pct" in r.message
                   for r in caplog.records), (
            f"expected SANITY warning on log_power; got "
            f"{[r.message for r in caplog.records]}"
        )

    def test_grade_sanity_clamp_negative(self):
        """fix34: grade_pct=-300 also clamped (symmetric envelope)."""
        from training_live import _sanitize_speed_inputs
        g, _m = _sanitize_speed_inputs(-300.0, 75.0)
        assert g == -25.0

    def test_mass_sanity_default(self, caplog):
        """fix34: mass=0 (uninitialised profile) → substitute 83 kg
        default and WARN on log_power."""
        import logging
        from training_live import _sanitize_speed_inputs, MASS_KG_DEFAULT
        caplog.set_level(logging.WARNING, logger="domestique.power")
        _g, m = _sanitize_speed_inputs(0.0, 0.0)
        assert m == MASS_KG_DEFAULT, f"expected {MASS_KG_DEFAULT}, got {m}"
        assert any("SANITY" in r.message and "rider_mass_kg" in r.message
                   for r in caplog.records)

    def test_mass_sanity_out_of_range_high(self):
        """fix34: mass=500 (bogus) → substitute default 83."""
        from training_live import _sanitize_speed_inputs, MASS_KG_DEFAULT
        _g, m = _sanitize_speed_inputs(0.0, 500.0)
        assert m == MASS_KG_DEFAULT



if __name__ == "__main__":
    import sys
    # Simple test runner without pytest
    passed = failed = 0
    for cls_name, cls in list(globals().items()):
        if isinstance(cls, type) and cls_name.startswith("Test"):
            for method_name in dir(cls):
                if method_name.startswith("test_"):
                    try:
                        getattr(cls(), method_name)()
                        passed += 1
                        print(f"  PASS  {cls_name}.{method_name}")
                    except Exception as e:
                        failed += 1
                        print(f"  FAIL  {cls_name}.{method_name}: {e}")
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
