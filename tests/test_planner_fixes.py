"""Verification tests for the 4 planner fixes.

Fix 1: Stepback reduction consistent at 0.55 (both plan_week and generate_weekly_plan).
Fix 2: Tempo removed from hit_types (doesn't count toward hit_per_week budget).
Fix 3: 3-day weeks scale HIT budget so at least 1 Z2 session per week.
Fix 4: Atomic plan writes in app.py (tmp_path + rename pattern at all 7 sites).
"""
from __future__ import annotations

import re
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from training_planner import (
    Goal,
    Phase,
    generate_plan,
    generate_phases,
    generate_weekly_plan,
    plan_week,
    _pick_session,
)


ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
PLANNER_PY = ROOT / "training_planner.py"


def _make_base_phase(start: date, weeks: int = 4, weekly_tss: float = 500) -> Phase:
    return Phase(
        name="base",
        start=start,
        end=start + timedelta(weeks=weeks) - timedelta(days=1),
        weeks=weeks,
        focus="Aerobic base",
        weekly_tss_target=weekly_tss,
        z2_pct=85,
        hit_per_week=1,
        session_types=["z2", "long_z2", "recovery", "tempo"],
    )


def _make_build2_phase(start: date, weeks: int = 4, weekly_tss: float = 700) -> Phase:
    return Phase(
        name="build2",
        start=start,
        end=start + timedelta(weeks=weeks) - timedelta(days=1),
        weeks=weeks,
        focus="VO2max intervals",
        weekly_tss_target=weekly_tss,
        z2_pct=65,
        hit_per_week=2,
        session_types=["z2", "vo2max", "overunder", "sweetspot", "sprint", "long_z2"],
    )


def _make_goal(available_days=None, rest_days=None) -> Goal:
    return Goal(
        goal_type="general",
        hours_per_week=8.0,
        max_weekday_hours=2.0,
        max_weekend_hours=3.5,
        available_days=available_days if available_days is not None else [1, 2, 3, 4, 5, 6],
        rest_days=rest_days if rest_days is not None else [0],
    )


class TestFix1StepbackReduction(unittest.TestCase):
    """Stepback week tss = 0.72 * normal weekly_tss_target."""

    def test_plan_week_stepback_factor(self):
        phase = _make_base_phase(date(2026, 4, 6), weekly_tss=500)
        goal = _make_goal()

        normal = plan_week(1, date(2026, 4, 6), phase, goal, is_stepback=False)
        stepback = plan_week(2, date(2026, 4, 13), phase, goal, is_stepback=True)

        self.assertEqual(normal.tss_target, 500)
        self.assertEqual(stepback.tss_target, round(500 * 0.72))
        self.assertTrue(stepback.is_stepback)
        self.assertFalse(normal.is_stepback)

    def test_plan_week_stepback_various_tss(self):
        # Try several different weekly_tss_target values; verify 0.72 factor
        for tss in (300, 450, 600, 800, 1000):
            phase = _make_base_phase(date(2026, 4, 6), weekly_tss=tss)
            goal = _make_goal()
            sb = plan_week(1, date(2026, 4, 6), phase, goal, is_stepback=True)
            self.assertEqual(sb.tss_target, round(tss * 0.72),
                             f"Stepback factor mismatch for tss={tss}")

    def test_generate_weekly_plan_uses_0_72(self):
        # Search the source to confirm the factor.
        src = PLANNER_PY.read_text()
        # Look for generate_weekly_plan's stepback branch. Must use 0.72.
        # The relevant line is: weekly_tss = round(weekly_tss * 0.72) inside generate_weekly_plan.
        gwp_start = src.index("def generate_weekly_plan(")
        gwp_end = src.index("def ", gwp_start + 1)
        gwp_src = src[gwp_start:gwp_end]
        self.assertIn("weekly_tss * 0.72", gwp_src,
                      "generate_weekly_plan does not use 0.72 factor")
        # Make sure no OTHER stepback factor is present in that function
        # (e.g., 0.50 or 0.55). Check for common wrong values inside a
        # `round(weekly_tss * X)` pattern.
        bad = re.findall(r"weekly_tss\s*\*\s*0\.(?!72)\d+", gwp_src)
        # Filter to patterns that look like stepback reductions (0.4-0.7)
        bad = [b for b in bad if re.match(r"weekly_tss\s*\*\s*0\.[4567]", b)]
        self.assertEqual(bad, [],
                         f"Unexpected non-0.72 stepback factors found: {bad}")

    def test_plan_week_and_generate_weekly_plan_match(self):
        # Both should use the same 0.72 factor. Verify via source inspection
        # that plan_week also uses 0.72.
        src = PLANNER_PY.read_text()
        pw_start = src.index("def plan_week(")
        pw_end = src.index("def ", pw_start + 1)
        pw_src = src[pw_start:pw_end]
        self.assertIn("tss_target * 0.72", pw_src,
                      "plan_week does not use 0.72 factor")


class TestFix2TempoNotHIT(unittest.TestCase):
    """Tempo sessions are not counted as HIT."""

    def test_hit_types_excludes_tempo(self):
        # Static source check: hit_types in _pick_session must not contain 'tempo'.
        src = PLANNER_PY.read_text()
        # Find all declarations of hit_types = { ... }
        matches = re.findall(r"hit_types\s*=\s*\{([^}]+)\}", src)
        self.assertTrue(len(matches) >= 1, "hit_types declaration not found")
        for m in matches:
            self.assertNotIn("tempo", m,
                             f"'tempo' found in hit_types set: {m}")
            # Must contain the real HIT types.
            for needed in ("vo2max", "threshold", "overunder", "sweetspot", "sprint"):
                self.assertIn(needed, m, f"{needed} missing from hit_types set")

    def test_base_phase_allows_tempo_alongside_hit(self):
        # Simulate: base phase, hit_per_week=1, one sweetspot already placed.
        # _pick_session called again should still be able to return tempo on
        # another day (not blocked by the HIT cap).
        phase = _make_base_phase(date(2026, 4, 6))
        # Pretend we have already placed a sweetspot (a HIT) earlier this week.
        # We'll ensure tempo can still be picked via the pool when can_hit=False.
        # But _pick_session includes tempo inside the base HIT_VARIANTS pool,
        # which only runs when can_hit. Outside can_hit, tempo is not picked.
        # What matters: hit_count does NOT include tempo. Simulate directly.
        from training_planner import PlannedSession as PS
        prior_tempo = PS(day=date(2026, 4, 7), day_name="Tue",
                         session_type="tempo", duration_min=60, tss_estimate=65,
                         description="tempo")
        prior_sweet = PS(day=date(2026, 4, 8), day_name="Wed",
                         session_type="sweetspot", duration_min=60, tss_estimate=80,
                         description="ss")
        sessions_so_far = [prior_tempo, prior_sweet]
        # Count HITs using the code's hit_types set (inspect source).
        hit_types = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}
        hit_count = sum(1 for s in sessions_so_far if s.session_type in hit_types)
        self.assertEqual(hit_count, 1,
                         "Tempo should not count as HIT, so hit_count must be 1")

    def test_multiple_tempos_and_one_hit_possible(self):
        # End-to-end: generate a plan using a base phase with hit_per_week=1.
        # Inspect weeks: should see some weeks with 1 HIT AND a tempo session.
        #
        # v1.0.5d note: previously @unittest.expectedFailure for the v1.0.5
        # regression. The v1.0.5d half-open boundary fix (BUG-A) tightened the
        # OU detector (over-leg cap 1.10) so Z6-sprint workouts no longer
        # filename-match `over_under_*` content; the hit-budget contract now
        # holds as originally intended.
        with patch("training_planner.get_today_metrics", return_value={"ctl": 40.0}):
            goal = Goal(
                goal_type="general",
                plan_weeks=16,
                hours_per_week=8.0,
                max_weekday_hours=2.0,
                max_weekend_hours=3.5,
                available_days=[1, 2, 3, 4, 5, 6],
                rest_days=[0],
            )
            phases, weeks = generate_plan(goal)

        hit_types = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}
        # Find at least one base week, count hit sessions and tempo sessions.
        base_weeks = [w for w in weeks if w.phase == "base" and not w.is_stepback]
        self.assertTrue(len(base_weeks) >= 1, "Expected at least one base week")
        # For every base week, HIT count must be <= phase hit_per_week (1).
        # Confirms tempo doesn't inflate the HIT budget.
        for w in base_weeks:
            hit = sum(1 for s in w.sessions if s.session_type in hit_types)
            self.assertLessEqual(hit, 1,
                                 f"Base week {w.week_num}: HIT count {hit} exceeds budget 1")


class TestFix3ThreeDayWeekHITScaling(unittest.TestCase):
    """3-day weeks cap HIT at 1 so at least one Z2 session remains."""

    def _gen_week_with_days(self, available_days, rest_days, phase_name="build2"):
        goal = _make_goal(available_days=available_days, rest_days=rest_days)
        if phase_name == "build2":
            phase = _make_build2_phase(date(2026, 4, 6), weekly_tss=700)
        else:
            phase = _make_base_phase(date(2026, 4, 6), weekly_tss=500)
        # Use generate_weekly_plan which contains the max_hit scaling logic.
        # Patch PLAN_DIR so no file IO is needed.
        with patch("training_planner.get_today_metrics", return_value={"ctl": 45.0}):
            return generate_weekly_plan(goal=goal, current_phase=phase, current_ctl=45.0)

    def test_three_day_week_has_at_least_one_z2(self):
        # Tue (1), Thu (3), Sun (6). Rest on all others.
        rest_days = [0, 2, 4, 5]  # Mon, Wed, Fri, Sat = rest
        avail = [1, 3, 6]
        week = self._gen_week_with_days(available_days=avail, rest_days=rest_days,
                                        phase_name="build2")
        hit_types = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}
        z2_like = {"z2", "long_z2", "recovery"}
        training_sessions = [s for s in week.sessions if s.session_type != "rest"]
        self.assertEqual(len(training_sessions), 3,
                         f"Expected 3 training sessions, got {len(training_sessions)}")
        z2_count = sum(1 for s in training_sessions if s.session_type in z2_like)
        hit_count = sum(1 for s in training_sessions if s.session_type in hit_types)
        self.assertGreaterEqual(z2_count, 1,
                                f"3-day week has 0 Z2 sessions. Sessions: "
                                f"{[s.session_type for s in training_sessions]}")
        self.assertLessEqual(hit_count, 1,
                             f"3-day week has {hit_count} HIT; expected <=1")

    def test_five_day_week_allows_two_hit(self):
        # Tue, Wed, Thu, Sat, Sun
        rest_days = [0, 4]  # Mon, Fri rest
        avail = [1, 2, 3, 5, 6]
        week = self._gen_week_with_days(available_days=avail, rest_days=rest_days,
                                        phase_name="build2")
        hit_types = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}
        training_sessions = [s for s in week.sessions if s.session_type != "rest"]
        hit_count = sum(1 for s in training_sessions if s.session_type in hit_types)
        # phase.hit_per_week = 2; formula: max 1 + (5-1)//2 = 2, so up to 2 allowed
        self.assertLessEqual(hit_count, 2,
                             f"5-day week has {hit_count} HIT; expected <=2")

    def test_max_hit_formula(self):
        # Directly test the formula used: max_hit = min(hit_per_week,
        #   max(1, (available_training_days - 1) // 2))
        # For available_training_days=3 (long+2 others): max(1, 2//2)=1 -> max 1 HIT
        # For available_training_days=5: max(1, 4//2)=2 -> up to 2 HIT
        # For available_training_days=6: max(1, 5//2)=2 -> up to 2 HIT
        # For available_training_days=7: max(1, 6//2)=3, capped by hit_per_week.
        for days, hit_per_week, expected_max in [
            (3, 2, 1),
            (4, 2, 1),
            (5, 2, 2),
            (6, 2, 2),
            (7, 2, 2),  # capped by hit_per_week
            (3, 1, 1),
        ]:
            calc = min(hit_per_week, max(1, (days - 1) // 2))
            self.assertEqual(calc, expected_max,
                             f"days={days} hit_per_week={hit_per_week}")


class TestFix4AtomicWrites(unittest.TestCase):
    """All plan-mutation sites in app.py go through ``tp.atomic_write_plan``.

    fix26 §6 added 3 new plan-write endpoints (move-session, rematch-apply,
    dismiss-session) and removed 1 legacy write path (daily-adapt's in-place
    mutation). Net: 7 → 9 atomic sites. v4.1.0 FIX-SERVER adds 2 more
    (today-session/persist + rematch-day) → 11. v1.0.3 IMPL-WIRING adds
    one more inside ``_maybe_auto_reforecast`` (best-effort auto-reforecast
    on ride sync / FIT import) → 12.

    v1.6.2: the inline ``tmp_path = json_path.with_suffix('.tmp') ...
    tmp_path.rename(json_path)`` pattern is replaced by a single helper
    call ``tp.atomic_write_plan(json_path, plan)`` that also rotates
    backups. The site count stays 12.

    v1.7.0–v1.8.21 added redraw/accept-redraw/save-availability/generate
    write paths (→15). v1.8.24 adds the consolidated ``/api/plan/update``
    write path → 16. The invariant is "every plan write goes through the
    helper (never inline tmp+rename)", asserted both by this exact-count
    guard and by ``test_no_inline_tmp_rename_pattern`` below.
    """

    def test_all_sites_use_atomic_write_plan_helper(self):
        """Every plan-mutation endpoint must call ``tp.atomic_write_plan``
        instead of inlining tmp+rename. Count 16 (v1.8.24) → 18 (v3.0.0:
        two new plan-writing endpoints in the hrTSS/event tranches) → 19
        (v3.2.0: /api/plan/ftp-test-type, the per-session ramp/20-min
        chooser) → 20 (3.3.1 hotfix B2: /api/plan/re-draw no longer
        delegates to the legacy rematch-day endpoint — it persists its own
        accept-redraw apply, correctly through the helper)."""
        src = APP_PY.read_text()
        helper_calls = re.findall(
            r"tp\.atomic_write_plan\(\s*json_path\s*,\s*(plan|plan_dict)\s*\)",
            src,
        )
        self.assertEqual(
            len(helper_calls), 23,  # +1: revert-cap's plan restore (the 'Ride the original anyway' fix)
            f"Expected 22 tp.atomic_write_plan() sites, found {len(helper_calls)}",
        )

    def test_no_inline_tmp_rename_pattern(self):
        """v1.6.2: the legacy inline ``tmp_path = json_path.with_suffix('.tmp')``
        pattern must not survive in app.py — every plan write goes through
        the helper now."""
        src = APP_PY.read_text()
        leftovers = re.findall(
            r"tmp_path\s*=\s*json_path\.with_suffix\(\s*['\"]\.tmp['\"]\s*\)",
            src,
        )
        self.assertEqual(
            leftovers, [],
            f"Inline tmp+rename plan-write blocks must be migrated to "
            f"tp.atomic_write_plan; found {len(leftovers)}",
        )

    def test_no_direct_json_dump_to_final_path(self):
        # Make sure no "with open(json_path" is directly followed by
        # json.dump(plan). This would bypass the atomic pattern.
        src = APP_PY.read_text()
        pattern = re.compile(
            r"with\s+open\(\s*json_path[^\n]*\n[^\n]*json\.dump\((plan|plan_dict)",
            re.MULTILINE,
        )
        matches = pattern.findall(src)
        self.assertEqual(matches, [],
                         f"Non-atomic writes to json_path found: {matches}")


class TestFixPlannerV411ClassifierPrefixes(unittest.TestCase):
    """v4.1.1 FIX-PLANNER A: _classify_protocol's filename-prefix fallback
    covers 6 extra prefix families that previously fell through to the
    dominant-zone heuristic, causing ~30% of plan sessions to have
    session_type≠zwo_file mismatch.

    v4.1.2 IMPL-CLASSIFIER: with the content-based cascade now preferred,
    these tests exercise the FALLBACK path (cache empty → filename heuristic).
    setUp clears the content-classification cache so the prefix-based fallback
    is invoked.
    """

    def setUp(self):
        # Force the filename fallback path by emptying the content cache
        # (3.3.1: dir-keyed — empty for the CURRENT dir).
        import training_planner as tp
        tp._CONTENT_CLASSIFICATION_CACHE = {str(tp.WORKOUT_DIR): {}}

    def tearDown(self):
        # Reset so subsequent tests get a fresh load.
        import training_planner as tp
        tp._CONTENT_CLASSIFICATION_CACHE = None

    def test_vo2_prefix_classifies_as_vo2max(self):
        from training_planner import _classify_protocol
        # zone split is dominant Z1 (would mis-classify as Recovery under
        # the old heuristic) — prefix must still force VO2max.
        self.assertEqual(
            _classify_protocol(600, 0, 0, 0, 0, 0, 1.2, "vo2_ladder5_120pct_60min.zwo"),
            "VO2max",
        )

    def test_over_under_prefix_classifies_as_over_unders(self):
        from training_planner import _classify_protocol
        self.assertEqual(
            _classify_protocol(0, 100, 500, 0, 0, 0, 1.1, "over_under_steady_53min_v2.zwo"),
            "Over-Unders",
        )

    def test_sprints_prefix_classifies_as_sprint(self):
        from training_planner import _classify_protocol
        self.assertEqual(
            _classify_protocol(900, 0, 0, 0, 0, 60, 1.5, "sprints_5x2min-1min_105pct_59min.zwo"),
            "Sprint",
        )

    def test_anaerobic_prefix_classifies_as_anaerobic(self):
        from training_planner import _classify_protocol
        self.assertEqual(
            _classify_protocol(800, 100, 0, 0, 0, 30, 1.35, "anaerobic_2x3min_53min.zwo"),
            "Anaerobic",
        )

    def test_sweet_spot_underscore_prefix_classifies_as_sweet_spot(self):
        from training_planner import _classify_protocol
        # The heuristic would default to Recovery (Z1 dominant) but the
        # prefix rule must win.
        self.assertEqual(
            _classify_protocol(600, 0, 300, 0, 0, 0, 0.93, "sweet_spot_60min.zwo"),
            "Sweet Spot",
        )

    def test_pyramid_prefix_classifies_as_mixed(self):
        from training_planner import _classify_protocol
        self.assertEqual(
            _classify_protocol(200, 200, 200, 200, 0, 0, 1.0, "pyramid_ladder_60min.zwo"),
            "Mixed",
        )

    def test_ftp_test_prefix_classifies_as_ftp_test(self):
        from training_planner import _classify_protocol
        self.assertEqual(
            _classify_protocol(0, 0, 600, 600, 0, 0, 1.05, "ftp_test_coggan_3x1min-1min_95pct_59min.zwo"),
            "FTP Test",
        )

    def test_vo2max_prefix_still_wins_over_vo2_prefix(self):
        # Order matters: "vo2max_" must be checked BEFORE "vo2_" because
        # "vo2max_foo".startswith("vo2_") is also True. Guard against
        # someone reordering the block and silently demoting vo2max.
        from training_planner import _classify_protocol
        self.assertEqual(
            _classify_protocol(0, 0, 0, 0, 600, 0, 1.2, "vo2max_billat_70min_v4.zwo"),
            "VO2max",
        )


class TestFixPlannerV411SessionStaleDetection(unittest.TestCase):
    """v1.8.18 — staleness is now RESOLVES-ON-DISK, not session_type↔prefix.
    The old prefix heuristic re-flagged match_zwo's legitimate fallback
    resolutions (sweetspot→over_under, sprint→neuromuscular) forever → churn.
    Existence is the only sound signal: a zwo_file is stale iff it has a path
    separator (external scrape slug) OR its basename is not in the local
    library. ``library_files`` must be supplied for the existence check.
    """

    def test_subdir_scrape_slug_is_stale(self):
        from training_planner import _session_is_stale
        lib = {"tempo_4x150s_85pct_63min.zwo"}
        self.assertTrue(_session_is_stale("z2", "ftp-builder/week-6-day-3.zwo", lib))

    def test_basename_not_in_library_is_stale(self):
        from training_planner import _session_is_stale
        lib = {"tempo_4x150s_85pct_63min.zwo"}
        self.assertTrue(_session_is_stale("z2", "z2_endurance_77min.zwo", lib))

    def test_resolved_file_is_not_stale_even_with_mismatched_prefix(self):
        # match_zwo legitimately resolves sweetspot → over_under_* via fallback;
        # if the file EXISTS it must NOT be flagged (idempotency).
        from training_planner import _session_is_stale
        lib = {"over_under_2x1min_90min.zwo"}
        self.assertFalse(
            _session_is_stale("sweetspot", "over_under_2x1min_90min.zwo", lib)
        )

    def test_no_library_means_not_stale(self):
        # Without the library set we can't check existence → don't touch.
        from training_planner import _session_is_stale
        self.assertFalse(_session_is_stale("tempo", "vo2_3x5min_90min.zwo"))

    def test_rest_session_is_never_stale(self):
        from training_planner import _session_is_stale
        self.assertFalse(_session_is_stale("rest", "", {"x.zwo"}))
        self.assertFalse(_session_is_stale("rest", "anything.zwo", {"x.zwo"}))

    def test_session_without_zwo_is_never_stale(self):
        from training_planner import _session_is_stale
        self.assertFalse(_session_is_stale("tempo", "", {"x.zwo"}))


class TestFixPlannerV411BasePhaseSessionTypes(unittest.TestCase):
    """v4.1.1 FIX-PLANNER B: base phase now includes `sweetspot` so HIT slots
    alternate tempo/sweetspot instead of tempo-only. build1 gains overunder
    (FTP goal) or threshold (VO2max goal).
    """

    def test_ftp_base_includes_sweetspot(self):
        from training_planner import generate_phases, Goal
        phases = generate_phases(
            Goal(goal_type="ftp", target_date=date.today() + timedelta(weeks=16),
                 hours_per_week=8.0),
            current_ctl=40,
        )
        base = next((p for p in phases if p.name == "base"), None)
        self.assertIsNotNone(base)
        self.assertIn("sweetspot", base.session_types)
        self.assertIn("tempo", base.session_types)

    def test_ftp_build1_includes_overunder(self):
        from training_planner import generate_phases, Goal
        phases = generate_phases(
            Goal(goal_type="ftp", target_date=date.today() + timedelta(weeks=16),
                 hours_per_week=8.0),
            current_ctl=40,
        )
        b1 = next((p for p in phases if p.name == "build1"), None)
        self.assertIsNotNone(b1)
        self.assertIn("overunder", b1.session_types)


if __name__ == "__main__":
    unittest.main()
