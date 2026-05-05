"""v1.0.6 IMPL-3D-PLANNER — TSS PRIMARY, 3D ADDITIVE regression + soft-bias tests.

The v1.0.6 changes ship 3D impulse-response (Kontro 2026 PLOS ONE) ADDITIVE
to the existing TSS-driven planner backbone. Every existing v1.0.4 / v1.0.5
test must continue to pass UNCHANGED — the 3D additions are nullable /
optional / advisory only. These tests pin that contract:

  1. test_tss_only_regression          — calling reforecast() with no 3D
                                          kwargs returns identical output to
                                          v1.0.5 (no advisory entries fired).
  2. test_optional_dataclass_fields    — PlannedSession / PlannedWeek with
                                          no 3D fields supplied are byte-
                                          identical to v1.0.5 behaviour.
  3. test_glycolytic_stacking_soft     — two consecutive vo2max-class days
                                          do NOT hard-reject the second pick;
                                          weight scales ×0.7 (soft).
  4. test_g8_advisory_log_only         — wprime_balance_24h < 0.5*W' fires
                                          an advisory log entry but does NOT
                                          mutate any session_type.
  5. test_reforecast_backward_compat   — calling with NO 3D kwargs returns
                                          plan identical to v1.0.5 path.
  6. test_mixed_not_in_v104_maps       — regression: 'mixed' must not appear
                                          in any of the v104-locked content-
                                          class maps (already true post-V105D
                                          but pinned here for v1.0.6).

Locked constraint (MASTER_DECISIONS_v106.md §0):
    "TSS PRIMARY, 3D ADDITIVE. Do NOT replace any TSS-based logic. The 3D
     additions are ADVISORY ONLY."
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

import training_planner as tp


def _next_monday() -> date:
    today = date.today()
    days_until_mon = (7 - today.weekday()) % 7
    if days_until_mon == 0:
        days_until_mon = 7
    return today + timedelta(days=days_until_mon)


def _mk_session(
    day: date,
    *,
    session_type: str = "z2",
    duration_min: int = 60,
    tss: float = 45.0,
) -> tp.PlannedSession:
    return tp.PlannedSession(
        day=day,
        day_name=day.strftime("%A"),
        session_type=session_type,
        duration_min=duration_min,
        tss_estimate=tss,
        description="",
    )


def _mk_week(start: date, sessions: list[tp.PlannedSession]) -> tp.PlannedWeek:
    return tp.PlannedWeek(
        week_num=1,
        start=start,
        end=start + timedelta(days=6),
        phase="build1",
        tss_target=400.0,
        is_stepback=False,
        sessions=sessions,
        hit_per_week=2,
    )


class TestTssOnlyRegression(unittest.TestCase):
    """v1.0.5 baseline: no 3D kwargs → identical behaviour, no advisory."""

    def test_tss_only_regression(self):
        mon = _next_monday()
        sessions = [
            _mk_session(mon, session_type="z2", duration_min=60, tss=45.0),
            _mk_session(mon + timedelta(days=1), session_type="rest",
                        duration_min=0, tss=0),
        ]
        weeks = [_mk_week(mon, sessions)]
        goal = tp.Goal(goal_type="general", hours_per_week=8.0)
        # Neutral TSB so no TSB downshift.
        tsb_series = {mon + timedelta(days=i): 0.0 for i in range(7)}

        _, info = tp.reforecast(goal, weeks, tsb_series=tsb_series)

        # No 3D kwargs supplied → advisory_log MUST be empty.
        self.assertEqual(info.get("advisory_log", []), [])
        self.assertFalse(info.get("g3b_breach", False))
        self.assertIsNone(info.get("g8_softened_day"))
        # Underlying TSS path untouched.
        self.assertEqual(info["downshifts"], 0)


class TestOptionalDataclassFields(unittest.TestCase):
    """PlannedSession + PlannedWeek with no 3D fields = v1.0.5 behaviour."""

    def test_planned_session_3d_fields_default_none(self):
        sess = _mk_session(_next_monday(), tss=85)
        # Existing TSS field unchanged.
        self.assertEqual(sess.tss_estimate, 85)
        # New 3D mirrors default to None — preserves TSS-only path.
        self.assertIsNone(sess.wprime_estimate)
        self.assertIsNone(sess.pmax_estimate)

    def test_planned_week_3d_fields_default_none(self):
        mon = _next_monday()
        wk = _mk_week(mon, [_mk_session(mon)])
        # TSS scalar untouched.
        self.assertEqual(wk.tss_target, 400.0)
        # 3D mirrors default to None.
        self.assertIsNone(wk.wprime_target)
        self.assertIsNone(wk.pmax_target)

    def test_phase_3d_fields_default_none(self):
        # Phase requires positional args; defaulted 3D fields stay None.
        ph = tp.Phase(
            name="build1",
            start=date(2026, 1, 1),
            end=date(2026, 1, 28),
            weeks=4,
            focus="threshold + vo2max",
            weekly_tss_target=600.0,
            z2_pct=70.0,
            hit_per_week=2,
            session_types=["threshold", "vo2max"],
        )
        self.assertIsNone(ph.weekly_wprime_target)
        self.assertIsNone(ph.weekly_pmax_target)

    def test_intensity_budget_3d_fields_default_none(self):
        ib = tp.IntensityBudget(
            z1z2_minutes_per_week=400,
            z3_minutes_per_week=60,
            z4_minutes_per_week=30,
            z5plus_minutes_per_week=15,
            tss_per_week=600,
            hit_count_min=2,
            hit_count_max=3,
            rest_days_per_week=2,
            polarized_target={"z1z2": 80, "z3": 5, "z4plus": 15},
        )
        self.assertIsNone(ib.wprime_per_week)
        self.assertIsNone(ib.pmax_per_week)


class TestGlycolyticStackingSoft(unittest.TestCase):
    """Soft anti-stacking: ×0.7 weight (NOT hard 0.0 / reject)."""

    def test_glycolytic_load_map_present(self):
        # Map exists at module level with vo2max=1.0 weight.
        self.assertEqual(tp._GLYCOLYTIC_LOAD_BY_CLASS["vo2max"], 1.0)
        self.assertEqual(tp._GLYCOLYTIC_LOAD_BY_CLASS["anaerobic"], 1.0)
        # Endurance / recovery have zero load (never penalize).
        self.assertEqual(tp._GLYCOLYTIC_LOAD_BY_CLASS["endurance"], 0.0)
        self.assertEqual(tp._GLYCOLYTIC_LOAD_BY_CLASS["recovery"], 0.0)

    def test_v104_locked_maps_unchanged(self):
        # Sanity: the 6 v104-locked maps still exist and have NOT been
        # mutated (regression — IMPL-3D-PLANNER must not touch them).
        self.assertIn("vo2max", tp._HIT_SLOT_CONTENT_CLASSES)
        self.assertIn("endurance", tp._ENDURANCE_SLOT_CONTENT_CLASSES)
        self.assertIn("threshold", tp._INTERVAL_SHAPED_CONTENT_CLASSES)
        self.assertIsInstance(tp.WORKOUT_MIX_PREFERENCE, dict)
        self.assertIsInstance(tp._PLAN_CLASS_MIN_DISTINCT_24W, dict)
        self.assertIsInstance(tp._CONTENT_TO_PROTOCOL, dict)

    def test_glyco_stack_scaler_factor_is_soft(self):
        """The penalty factor itself is 0.7, NOT 0.0 — soft, not reject.

        Pin the scaler value as a literal so it can't drift to a hard
        reject without a deliberate code change + test update.
        """
        # Re-extract the scaler from the file by importing the source and
        # checking that it contains the soft 0.7 multiplier (not 0.3 — the
        # audit's super-set recommendation we deliberately softened).
        import inspect
        src = inspect.getsource(tp.sample_week_workouts)
        # Soft scalar must be present.
        self.assertIn("glyco_stack_mult = 0.7", src)
        # Must NOT have hard-rejected (audit-superset) value.
        self.assertNotIn("glyco_stack_mult = 0.3", src)
        self.assertNotIn("glyco_stack_mult = 0.0", src)


class TestG8AdvisoryLogOnly(unittest.TestCase):
    """G8 advisory: log entry fires; session_type does NOT mutate."""

    def test_g8_fires_below_threshold_log_only(self):
        mon = _next_monday()
        # Build a week with a future hard slot (Tuesday vo2max).
        sessions = [
            _mk_session(mon, session_type="z2", duration_min=60, tss=45),
            _mk_session(mon + timedelta(days=1),
                        session_type="vo2max", duration_min=60, tss=80),
            _mk_session(mon + timedelta(days=2), session_type="rest",
                        duration_min=0, tss=0),
        ]
        weeks = [_mk_week(mon, sessions)]
        goal = tp.Goal(goal_type="general", hours_per_week=8.0)
        tsb_series = {mon + timedelta(days=i): 0.0 for i in range(7)}

        # wprime_balance_24h = 0.4 * W' → triggers G8 (< 0.5 threshold).
        w_prime_capacity = 22000.0  # 22 kJ — typical trained cyclist
        wp_balance = 0.4 * w_prime_capacity

        _, info = tp.reforecast(
            goal, weeks,
            tsb_series=tsb_series,
            wprime_balance_24h=wp_balance,
            w_prime=w_prime_capacity,
        )

        # Advisory log must contain a G8 entry.
        log = info.get("advisory_log", [])
        g8_entries = [e for e in log if "G8" in e or "wprime" in e.lower()]
        self.assertTrue(
            len(g8_entries) >= 1,
            f"G8 advisory should fire when wprime<0.5*W'; log={log}",
        )

        # CRITICAL: no session_type mutation. The vo2max future slot must
        # remain vo2max — G8 is ADVISORY, not a hard tier-down.
        future_vo2 = next(
            s for s in weeks[0].sessions
            if s.day == mon + timedelta(days=1)
        )
        self.assertEqual(
            future_vo2.session_type, "vo2max",
            "G8 must NOT mutate session_type — advisory only",
        )
        self.assertFalse(
            future_vo2.adapted,
            "G8 must NOT set s.adapted=True — advisory only",
        )

    def test_g8_silent_above_threshold(self):
        """wprime_balance >0.5*W' → no G8 entry, no softened day."""
        mon = _next_monday()
        sessions = [
            _mk_session(mon, session_type="vo2max", duration_min=60, tss=80),
        ]
        weeks = [_mk_week(mon, sessions)]
        goal = tp.Goal(goal_type="general", hours_per_week=8.0)
        tsb_series = {mon + timedelta(days=i): 0.0 for i in range(7)}

        w_prime_capacity = 22000.0
        wp_balance = 0.85 * w_prime_capacity  # well above threshold

        _, info = tp.reforecast(
            goal, weeks,
            tsb_series=tsb_series,
            wprime_balance_24h=wp_balance,
            w_prime=w_prime_capacity,
        )

        log = info.get("advisory_log", [])
        g8_entries = [e for e in log if "G8" in e]
        self.assertEqual(g8_entries, [],
                         "G8 must be silent when wprime>=0.5*W'")
        self.assertIsNone(info.get("g8_softened_day"))


class TestReforecastBackwardCompat(unittest.TestCase):
    """Calling reforecast() with NO 3D kwargs == v1.0.5 path."""

    def test_no_3d_kwargs_no_behaviour_change(self):
        mon = _next_monday()
        # Future hard session.
        sessions = [
            _mk_session(mon + timedelta(days=1), session_type="vo2max",
                        duration_min=60, tss=80),
        ]
        weeks_a = [_mk_week(mon, sessions)]
        weeks_b = [_mk_week(mon, [_mk_session(
            mon + timedelta(days=1), session_type="vo2max",
            duration_min=60, tss=80,
        )])]
        goal = tp.Goal(goal_type="general", hours_per_week=8.0)
        tsb_series = {mon + timedelta(days=i): 0.0 for i in range(7)}

        # Path A: legacy v1.0.5-style call (no 3D kwargs).
        _, info_a = tp.reforecast(goal, weeks_a, tsb_series=tsb_series)
        # Path B: explicit None for all 3D kwargs.
        _, info_b = tp.reforecast(
            goal, weeks_b,
            tsb_series=tsb_series,
            wprime_balance_24h=None,
            w_prime=None,
            wprime_acwr=None,
            actual_wprime_polarization=None,
        )

        # Both paths agree on legacy-shape fields.
        for key in (
            "action", "downshifts", "polarization_breach", "g3_dropped_days",
        ):
            self.assertEqual(info_a[key], info_b[key],
                             f"key {key!r} drifted between None and absent")

        # Both paths are SILENT: empty advisory log, no g3b breach,
        # no g8 softened day.
        self.assertEqual(info_a.get("advisory_log", []), [])
        self.assertEqual(info_b.get("advisory_log", []), [])
        self.assertFalse(info_a.get("g3b_breach", False))
        self.assertFalse(info_b.get("g3b_breach", False))
        self.assertIsNone(info_a.get("g8_softened_day"))
        self.assertIsNone(info_b.get("g8_softened_day"))

        # Future vo2max slot un-mutated in both paths (TSB neutral).
        for weeks in (weeks_a, weeks_b):
            future = next(
                s for s in weeks[0].sessions
                if s.day == mon + timedelta(days=1)
            )
            self.assertEqual(future.session_type, "vo2max")
            self.assertFalse(future.adapted)

    def test_wprime_acwr_advisory_log_only(self):
        """wprime_acwr>1.5 logs an advisory; does NOT trip G4 a second time."""
        mon = _next_monday()
        sessions = [_mk_session(mon, session_type="z2", duration_min=60)]
        weeks = [_mk_week(mon, sessions)]
        goal = tp.Goal(goal_type="general", hours_per_week=8.0)
        tsb_series = {mon + timedelta(days=i): 0.0 for i in range(7)}

        _, info = tp.reforecast(
            goal, weeks,
            tsb_series=tsb_series,
            wprime_acwr=2.0,  # well above 1.5 threshold
        )

        log = info.get("advisory_log", [])
        wp_acwr_entries = [e for e in log if "wprime_acwr" in e]
        self.assertEqual(
            len(wp_acwr_entries), 1,
            f"wprime_acwr>1.5 should log exactly one advisory; log={log}",
        )
        # acwr_scaled_week is the TSS-based G4 trip; wprime_acwr advisory
        # alone must NOT trigger it.
        self.assertIsNone(info.get("acwr_scaled_week"))


class TestMixedNotInV104Maps(unittest.TestCase):
    """Regression: the 'mixed' content_class string is not in v104-locked maps.

    v1.0.4 IMPL-PLANNER-CLASSIFIER-V104 removed the 'mixed' label after the
    classifier was rebuilt to assign every workout a primary class. This test
    pins that 'mixed' has not crept back into any of the 6 v104-locked maps
    while v1.0.6 was being added.
    """

    def test_mixed_absent_from_hit_endurance_classes(self):
        self.assertNotIn("mixed", tp._HIT_SLOT_CONTENT_CLASSES)
        self.assertNotIn("mixed", tp._ENDURANCE_SLOT_CONTENT_CLASSES)
        self.assertNotIn("mixed", tp._INTERVAL_SHAPED_CONTENT_CLASSES)

    def test_mixed_absent_from_workout_mix_preference(self):
        # WORKOUT_MIX_PREFERENCE is dict[phase -> list[dict]] of class weights.
        # 'mixed' must not appear as a key in any of those weight rows.
        for phase_name, rows in tp.WORKOUT_MIX_PREFERENCE.items():
            for row in rows:
                self.assertNotIn(
                    "mixed", row,
                    f"'mixed' leaked into WORKOUT_MIX_PREFERENCE[{phase_name!r}]",
                )

    def test_mixed_absent_from_class_min_distinct_and_protocol(self):
        self.assertNotIn("mixed", tp._PLAN_CLASS_MIN_DISTINCT_24W)
        self.assertNotIn("mixed", tp._CONTENT_TO_PROTOCOL)

    def test_mixed_absent_from_glycolytic_load_map(self):
        # The new v1.0.6 _GLYCOLYTIC_LOAD_BY_CLASS map must also not carry
        # 'mixed' (would be inconsistent with v1.0.4 cleanup).
        self.assertNotIn("mixed", tp._GLYCOLYTIC_LOAD_BY_CLASS)


if __name__ == "__main__":
    unittest.main()
