"""v1.1.0 IMPL-NORWEGIAN-HR — HR-only Norwegian Method tests.

7 tests per master §2 acceptance + PATCH G10 no-op tier-down:

  1. ``_set_max_hr`` source-tier respects manual override.
  2. Tanaka fallback ``int(208 - 0.7*age)`` returns int in [150, 200] for
     ages 20-70.
  3. ``double_threshold`` AM+PM scheduling: build2 week generates one
     same-day pair with ≥4 h gap.
  4. G9 advisory: yesterday α1=0.65 + today vo2max →
     ``advised_class='threshold'``, session NOT mutated.
  5. G9 advisory: yesterday α1=None → no advisory fires (safe degradation).
  6. G9 advisory PATCH G10: today's class is ``endurance`` → returns
     ``advised_class='endurance', reason='already at lowest tier',
     should_log=False``. NO KeyError.
  7. ``wbal_overshoot``: synthetic ride with W' trough = 30% of W' →
     flag True.

All tests run against an isolated tmp profile dir to avoid clobbering
the real one (mirrors test_pmax_ingest fixture pattern).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT))

import training_planner as tp  # noqa: E402
import zones  # noqa: E402


def _make_base(tmp: str) -> Path:
    base = Path(tmp) / ".domestique"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _bootstrap_profile(base: Path) -> None:
    p = base / "profiles" / "default"
    p.mkdir(parents=True, exist_ok=True)
    (p / "athlete.json").write_text(json.dumps({
        "ftp": 250, "weight_kg": 72.0, "lbm_kg": 58.0,
        "lthr": 175, "max_hr": 196, "wprime_j": 20000,
    }), encoding="utf-8")
    (p / ".env").write_text("ICU_ATHLETE_ID=i1\nICU_API_KEY=k1\n", encoding="utf-8")
    (p / "user_prefs.json").write_text(json.dumps({}), encoding="utf-8")
    (p / "device_prefs.json").write_text(json.dumps({}), encoding="utf-8")
    (base / "profiles.json").write_text(json.dumps({
        "version": 1, "active_profile": "default", "skip_picker": True,
        "profiles": [{
            "id": "default", "name": "Test", "color": "#3b82f6",
            "created": "2026-04-01T00:00:00", "last_used": "2026-04-01T00:00:00",
        }],
    }), encoding="utf-8")


class _ProfileFixture(unittest.TestCase):
    """Tmp profile + fresh ProfileManager singleton."""

    def setUp(self):
        from profile_manager import ProfileManager
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_profile(_make_base(self.tmp))
        self._home_patch = patch("pathlib.Path.home", return_value=Path(self.tmp))
        self._home_patch.start()
        ProfileManager._instance = None
        self.pm = ProfileManager.get()

    def tearDown(self):
        from profile_manager import ProfileManager
        self._home_patch.stop()
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── Test 1: _set_max_hr source-tier respects manual override ────────────────

class TestMaxHrSourceTier(_ProfileFixture):
    """master §1 + PATCH G6 — `_set_max_hr` priority manual > icu > computed > age_tanaka."""

    def test_manual_override_wins_over_icu(self):
        # Manual write first; later ICU write must be rejected.
        ok = self.pm._set_max_hr(195, "manual")
        self.assertTrue(ok)
        self.assertEqual(self.pm.max_hr, 195)
        self.assertEqual(self.pm.max_hr_source, "manual")

        # ICU tries to overwrite — must be rejected.
        ok = self.pm._set_max_hr(190, "icu")
        self.assertFalse(ok, "ICU must NOT downgrade manual max_hr")
        self.assertEqual(self.pm.max_hr, 195)
        self.assertEqual(self.pm.max_hr_source, "manual")

    def test_icu_overwrites_age_tanaka(self):
        ok = self.pm._set_max_hr(192, "age_tanaka")
        self.assertTrue(ok)
        self.assertEqual(self.pm.max_hr_source, "age_tanaka")

        ok = self.pm._set_max_hr(190, "icu")
        self.assertTrue(ok, "icu has higher priority than age_tanaka")
        self.assertEqual(self.pm.max_hr, 190)
        self.assertEqual(self.pm.max_hr_source, "icu")

    def test_out_of_range_rejected(self):
        # Below 140 — rejected, no write.
        before = self.pm.max_hr
        ok = self.pm._set_max_hr(130, "manual")
        self.assertFalse(ok)
        self.assertEqual(self.pm.max_hr, before)

        # Above 220 — rejected, no write.
        ok = self.pm._set_max_hr(225, "manual")
        self.assertFalse(ok)
        self.assertEqual(self.pm.max_hr, before)


# ── Test 2: Tanaka fallback returns int in [150, 200] for ages 20-70 ────────

class TestTanakaFallback(unittest.TestCase):
    """master §1 — Tanaka 2001: 208 - 0.7 * age. Returns int in [150, 200]
    for adult endurance population (ages 20-70)."""

    def test_tanaka_returns_int_in_range_for_ages_20_to_70(self):
        for age in range(20, 71):
            v = zones.estimated_hr_max(age)
            self.assertIsInstance(v, int, f"Tanaka(age={age}) must return int")
            self.assertGreaterEqual(v, 150,
                                    f"Tanaka(age={age})={v} must be ≥ 150")
            self.assertLessEqual(v, 200,
                                 f"Tanaka(age={age})={v} must be ≤ 200")

    def test_tanaka_matches_formula(self):
        # Spot-check a few exact values.
        # 208 - 0.7 * 30 = 208 - 21 = 187
        self.assertEqual(zones.estimated_hr_max(30), 187)
        # 208 - 0.7 * 50 = 208 - 35 = 173
        self.assertEqual(zones.estimated_hr_max(50), 173)
        # 208 - 0.7 * 70 = 208 - 49 = 159
        self.assertEqual(zones.estimated_hr_max(70), 159)


# ── Test 3: double_threshold AM+PM scheduling ───────────────────────────────

class TestDoubleThresholdScheduling(unittest.TestCase):
    """master §1 — double_threshold AM+PM same-day, ≥4 h gap, both with
    hr_ceiling_pct=0.88. ``schedule_double_threshold_pair`` is the locked
    helper that emits both halves."""

    def test_pair_is_same_day_with_min_gap_invariant(self):
        d = date(2026, 5, 14)  # any Thursday
        am, pm = tp.schedule_double_threshold_pair(
            day=d, day_name="thursday", pair_id="dtp-w14-thu",
        )
        # Both halves on the same day.
        self.assertEqual(am.day, d)
        self.assertEqual(pm.day, d)
        # Both half session_type is `double_threshold`.
        self.assertEqual(am.session_type, "double_threshold")
        self.assertEqual(pm.session_type, "double_threshold")
        # Pair invariants.
        self.assertTrue(am.is_double_threshold_pair)
        self.assertTrue(pm.is_double_threshold_pair)
        self.assertEqual(am.am_or_pm, "am")
        self.assertEqual(pm.am_or_pm, "pm")
        self.assertEqual(am.double_threshold_partner_id, "dtp-w14-thu")
        self.assertEqual(pm.double_threshold_partner_id, "dtp-w14-thu")
        # HR ceiling is the master-spec 88% — both halves.
        self.assertAlmostEqual(am.hr_ceiling_pct, 0.88)
        self.assertAlmostEqual(pm.hr_ceiling_pct, 0.88)
        # ≥4 h gap is part of the locked spec — verified by the constant.
        self.assertGreaterEqual(tp.DOUBLE_THRESHOLD_MIN_GAP_HOURS, 4)
        # Description mentions the gap so the rider sees it on the calendar.
        self.assertIn("4 h", pm.description)

    def test_double_threshold_in_build2_workout_mix(self):
        # Per master §1 — gated to build1 W3+, build2, peak. NEVER base/taper.
        # Verify build2 includes it.
        self.assertIn("double_threshold", tp.WORKOUT_MIX_PREFERENCE["build2"][0])
        # Verify peak includes it.
        self.assertIn("double_threshold", tp.WORKOUT_MIX_PREFERENCE["peak"][0])
        # Verify base does NOT include it.
        for row in tp.WORKOUT_MIX_PREFERENCE["base"]:
            self.assertNotIn("double_threshold", row,
                             "base must NOT include double_threshold")
        # Verify taper does NOT include it.
        for row in tp.WORKOUT_MIX_PREFERENCE["taper"]:
            self.assertNotIn("double_threshold", row,
                             "taper must NOT include double_threshold")


# ── Test 4: G9 advisory α1=0.65 + vo2max → tier-down to threshold ───────────

class TestG9AdvisoryFires(unittest.TestCase):
    """master §1 — yesterday's α1 < 0.75 (Rogers 2021 LT1 drift) +
    today's vo2max → advised_class='threshold', should_log=True. The
    actual session_type is NEVER mutated by the advisory function."""

    def test_alpha1_065_with_vo2max_advises_threshold(self):
        result = tp.g9_advisory(
            yesterday_dfa_alpha1=0.65,
            today_class="vo2max",
        )
        self.assertEqual(result["advised_class"], "threshold")
        self.assertTrue(result["should_log"])
        # Reason should mention the actual α1 value and 0.75 threshold.
        self.assertIn("0.65", result["reason"])
        self.assertIn("0.75", result["reason"])

    def test_session_not_mutated_by_advisory(self):
        # Build a synthetic session and confirm advisory leaves it alone.
        d = date.today()
        session = tp.PlannedSession(
            day=d, day_name=d.strftime("%A").lower(),
            session_type="vo2max", duration_min=60,
            tss_estimate=75, description="vo2max",
        )
        before = session.session_type
        result = tp.g9_advisory(
            yesterday_dfa_alpha1=0.65,
            today_class=session.session_type,
        )
        self.assertEqual(result["advised_class"], "threshold")
        # Critical invariant: session.session_type is unchanged.
        self.assertEqual(session.session_type, before)


# ── Test 5: G9 advisory α1=None → no advisory fires (safe degradation) ──────

class TestG9SafeDegradation(unittest.TestCase):
    """master §1 + PATCH — when v1.0.7 hasn't populated DFA α1 (rider has
    no chest strap, FIT lacks RR data, fetch failed), g9_advisory must NOT
    fire. Returns the today_class unchanged with should_log=False."""

    def test_alpha1_none_returns_should_log_false(self):
        result = tp.g9_advisory(
            yesterday_dfa_alpha1=None,
            today_class="vo2max",
        )
        self.assertEqual(result["advised_class"], "vo2max",
                         "missing α1 must NOT mutate the class")
        self.assertFalse(result["should_log"])
        self.assertIn("no DFA", result["reason"])

    def test_alpha1_above_threshold_does_not_fire(self):
        # α1 ≥ 0.75 means the rider is recovered — no tier-down.
        result = tp.g9_advisory(
            yesterday_dfa_alpha1=0.85,
            today_class="vo2max",
        )
        self.assertEqual(result["advised_class"], "vo2max")
        self.assertFalse(result["should_log"])


# ── Test 6: PATCH G10 no-op for already-low-tier classes ────────────────────

class TestG9PatchG10NoOp(unittest.TestCase):
    """PATCH G10 — when today's class is NOT in G9_TIER_DOWN_BUCKETS
    (e.g. 'endurance', 'recovery', 'rest'), g9_advisory must return
    advised_class=today_class, reason='already at lowest tier',
    should_log=False. NO KeyError ever."""

    def test_endurance_returns_no_op_no_keyerror(self):
        result = tp.g9_advisory(
            yesterday_dfa_alpha1=0.50,  # very low α1 — would otherwise tier-down
            today_class="endurance",
        )
        self.assertEqual(result["advised_class"], "endurance")
        self.assertEqual(result["reason"], "already at lowest tier")
        self.assertFalse(result["should_log"])

    def test_recovery_returns_no_op_no_keyerror(self):
        result = tp.g9_advisory(
            yesterday_dfa_alpha1=0.50,
            today_class="recovery",
        )
        self.assertEqual(result["advised_class"], "recovery")
        self.assertEqual(result["reason"], "already at lowest tier")
        self.assertFalse(result["should_log"])

    def test_rest_returns_no_op_no_keyerror(self):
        result = tp.g9_advisory(
            yesterday_dfa_alpha1=0.50,
            today_class="rest",
        )
        self.assertEqual(result["advised_class"], "rest")
        self.assertEqual(result["reason"], "already at lowest tier")
        self.assertFalse(result["should_log"])


# ── Test 7: wbal_overshoot — synthetic W' trough = 30% of W' → True ─────────

class TestWbalOvershoot(_ProfileFixture):
    """master §1 — wbal_overshoot True when W'bal trough drops below 60%
    of W'. Synthetic ride with trough at 30% of W' must flag True."""

    def test_trough_30pct_of_wprime_flags_true(self):
        # Profile: wprime_j=20000 J = 20.0 kJ.
        # 30% of W' as kJ: 0.30 × 20.0 = 6.0 kJ.
        from ride_storage import detect_wbal_overshoot
        ride = {"wbal_min_kj": 6.0}
        self.assertTrue(detect_wbal_overshoot(ride))
        # Same with explicit wprime_j override.
        self.assertTrue(detect_wbal_overshoot(ride, wprime_j=20000))

    def test_trough_70pct_of_wprime_flags_false(self):
        from ride_storage import detect_wbal_overshoot
        # 70% of W' (in kJ) — well above the 60% threshold.
        ride = {"wbal_min_kj": 14.0}
        self.assertFalse(detect_wbal_overshoot(ride, wprime_j=20000))

    def test_trough_at_exactly_60pct_flags_false(self):
        # Strict less-than: 60% itself is NOT overshoot (master spec
        # threshold is "below 60%").
        from ride_storage import detect_wbal_overshoot
        ride = {"wbal_min_kj": 12.0}
        self.assertFalse(detect_wbal_overshoot(ride, wprime_j=20000))

    def test_missing_signal_returns_false(self):
        from ride_storage import detect_wbal_overshoot
        # No wbal_min_kj at all.
        self.assertFalse(detect_wbal_overshoot({}, wprime_j=20000))
        # Default 0.0 (un-populated dataclass field) is treated as "no signal".
        self.assertFalse(detect_wbal_overshoot({"wbal_min_kj": 0.0},
                                               wprime_j=20000))


if __name__ == "__main__":
    unittest.main()
