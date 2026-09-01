"""3.4.0 W2 (IP_CONTINUOUS_MODE amendments C+D) — deload triggers + rotation.

C — the already-computed monotony/ACWR series advance the deload in
    continuous plans: monotony >= 2.0 or ACWR > 1.5 (grill P4) converts the
    CURRENT week to the deload shape via plan_week(is_stepback=True) +
    match_zwo, never two deloads back-to-back, revertible + week-latched
    (DFA auto-swap pattern).
D — continuous_policy.suggest_today_family: HRV-gated rotation policy
    (LnRMSSD7d vs SWC two-sided, TSB floor, 48h spacing + glyco day-after,
    weekly anaerobic dose) exposed as /api/today-session
    `continuous_suggestion` — additive, absent for finite goals.

Hermetic: policy fns are pure; app-level tests pin `today`, patch
metrics/rides/plan-dir, never touch the network (conftest gates), and the
tracked workouts/.library_index.json is snapshotted + restored around the
module (the planner rewrites it on load).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app as app_module
import continuous_policy as cpol
import training_planner as tp

_LIB_INDEX = Path(__file__).resolve().parent.parent / "src" / "workouts" / ".library_index.json"

HARD_TYPES = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}

GREEN = {"ln_rmssd_7d": 3.90, "swc_lower": 3.70, "swc_upper": 4.10, "tsb": 5.0}


@pytest.fixture(scope="module", autouse=True)
def _restore_library_index():
    backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None
    yield
    if backup is not None:
        _LIB_INDEX.write_bytes(backup)


# ═══════════════════════════════════════════════════════════════════════════
# D — policy fn truth table (pure, no I/O)
# ═══════════════════════════════════════════════════════════════════════════

def _suggest(focus="both", deficits=None, readiness=None, dsa=3):
    return cpol.suggest_today_family(
        focus_pref=focus, deficits=deficits or {},
        readiness=readiness if readiness is not None else dict(GREEN),
        days_since_last_anaerobic=dsa)


def _assert_shape(out):
    assert set(out) == {"family", "reason"}
    assert out["family"] in cpol.FAMILIES
    assert isinstance(out["reason"], str) and out["reason"].endswith(".")
    # one sentence: no mid-string sentence break
    assert ". " not in out["reason"].replace("e.g. ", "")


def test_hrv_below_band_forces_low_aerobic():
    r = dict(GREEN, ln_rmssd_7d=3.50)
    out = _suggest(deficits={"high_aerobic": 60, "anaerobic": 10}, readiness=r, dsa=9)
    _assert_shape(out)
    assert out["family"] == "low_aerobic"
    assert "below" in out["reason"].lower()


def test_hrv_above_band_forces_low_aerobic_two_sided():
    r = dict(GREEN, ln_rmssd_7d=4.30)
    out = _suggest(deficits={"high_aerobic": 60}, readiness=r, dsa=9)
    _assert_shape(out)
    assert out["family"] == "low_aerobic"
    assert "above" in out["reason"].lower()


def test_tsb_deep_fatigue_forces_low_aerobic():
    r = dict(GREEN, tsb=-30.0)
    out = _suggest(deficits={"high_aerobic": 60}, readiness=r, dsa=9)
    _assert_shape(out)
    assert out["family"] == "low_aerobic"
    assert "tsb" in out["reason"].lower()
    # boundary: exactly -25 does NOT trip (strictly below the floor)
    assert _suggest(deficits={"high_aerobic": 60},
                    readiness=dict(GREEN, tsb=-25.0))["family"] == "high_aerobic"


@pytest.mark.parametrize("dsa", [0, 1])
def test_recent_anaerobic_spacing_forces_low_aerobic(dsa):
    out = _suggest(deficits={"high_aerobic": 60, "anaerobic": 15}, dsa=dsa)
    _assert_shape(out)
    assert out["family"] == "low_aerobic"
    assert "48h" in out["reason"]


@pytest.mark.parametrize("dsa", [7, 12])
def test_anaerobic_overdue_wins_on_green_day(dsa):
    out = _suggest(deficits={"high_aerobic": 60, "anaerobic": 0}, dsa=dsa)
    _assert_shape(out)
    assert out["family"] == "anaerobic"
    assert f"{dsa}d" in out["reason"]


def test_high_aerobic_owed_green_day():
    out = _suggest(deficits={"high_aerobic": 40, "anaerobic": 0}, dsa=3)
    _assert_shape(out)
    assert out["family"] == "high_aerobic"
    assert "40min" in out["reason"]


def test_anaerobic_owed_only_green_day():
    out = _suggest(deficits={"high_aerobic": 0, "anaerobic": 12}, dsa=3)
    _assert_shape(out)
    assert out["family"] == "anaerobic"


@pytest.mark.parametrize("focus,family", [
    ("ftp", "high_aerobic"), ("both", "high_aerobic"), ("vo2", "anaerobic")])
def test_focus_pref_breaks_both_owed_tie(focus, family):
    out = _suggest(focus=focus,
                   deficits={"high_aerobic": 40, "anaerobic": 12}, dsa=3)
    _assert_shape(out)
    assert out["family"] == family


def test_budget_served_stays_low_aerobic():
    out = _suggest(deficits={"high_aerobic": 0, "anaerobic": -5,
                             "low_aerobic": 120}, dsa=3)
    _assert_shape(out)
    assert out["family"] == "low_aerobic"
    assert "80/20" in out["reason"]


def test_sub_floor_deficits_are_noise_not_debts():
    # 9min of Z3-Z5 / 1min of Z6+ are zone-edge wobble — never a hard call.
    assert _suggest(deficits={"high_aerobic": 9}, dsa=3)["family"] == "low_aerobic"
    assert _suggest(deficits={"anaerobic": 1}, dsa=3)["family"] == "low_aerobic"
    # ...while the family-scaled floors themselves DO count.
    assert _suggest(deficits={"high_aerobic": 10}, dsa=3)["family"] == "high_aerobic"
    assert _suggest(deficits={"anaerobic": 2}, dsa=3)["family"] == "anaerobic"


def test_unknown_hrv_falls_back_to_tsb_only():
    # No baseline + fresh TSB → hard allowed, but the reason says so.
    r = {"ln_rmssd_7d": None, "swc_lower": None, "swc_upper": None, "tsb": 5.0}
    out = _suggest(deficits={"high_aerobic": 40}, readiness=r, dsa=3)
    _assert_shape(out)
    assert out["family"] == "high_aerobic"
    assert "no HRV baseline" in out["reason"]
    # No baseline + deep fatigue → the TSB floor still protects.
    r2 = dict(r, tsb=-40.0)
    assert _suggest(deficits={"high_aerobic": 40},
                    readiness=r2, dsa=3)["family"] == "low_aerobic"


def test_dsa_none_neither_blocks_nor_flags_overdue():
    # None (nothing on record) can't block spacing NOR auto-fire overdue —
    # a fresh install must not open with "Anaerobic today".
    out = _suggest(deficits={"anaerobic": 12}, dsa=None)
    assert out["family"] == "anaerobic"          # owed → still reachable
    out2 = _suggest(deficits={}, dsa=None)
    assert out2["family"] == "low_aerobic"       # nothing owed → base


def test_deload_week_outranks_every_hard_rung():
    # Green HRV + overdue anaerobic + big debts — the deload still wins:
    # the suggestion must never contradict the (scheduled or advanced) deload.
    out = cpol.suggest_today_family(
        "vo2", {"high_aerobic": 60, "anaerobic": 15}, dict(GREEN),
        days_since_last_anaerobic=10, deload_week=True)
    _assert_shape(out)
    assert out["family"] == "low_aerobic"
    assert "deload" in out["reason"].lower()


def test_empty_inputs_never_crash():
    out = cpol.suggest_today_family("both", None, None, None)
    _assert_shape(out)
    assert out["family"] == "low_aerobic"


# ═══════════════════════════════════════════════════════════════════════════
# C — trigger fns (pure)
# ═══════════════════════════════════════════════════════════════════════════

def test_deload_trigger_thresholds():
    assert cpol.deload_trigger(None, None) is None
    assert cpol.deload_trigger(1.99, 1.50) is None       # both under the line
    trip = cpol.deload_trigger(2.0, None)                # monotony >= 2.0
    assert trip["trigger"] == "monotony" and trip["value"] == 2.0
    trip = cpol.deload_trigger(None, 1.51)               # ACWR strictly > 1.5
    assert trip["trigger"] == "acwr" and trip["value"] == 1.51
    trip = cpol.deload_trigger(2.4, 1.9)                 # monotony first (P4)
    assert trip["trigger"] == "monotony"
    assert trip["reason"].endswith(".")


def test_foster_monotony_math():
    assert cpol.foster_monotony([]) is None
    assert cpol.foster_monotony([0] * 7) is None
    assert cpol.foster_monotony([70] * 7) == cpol.MONOTONY_CAP  # SD=0, capped
    daily = [100, 0, 80, 0, 90, 0, 60]
    m = cpol.foster_monotony(daily)
    mean = sum(daily) / 7
    sd = (sum((v - mean) ** 2 for v in daily) / 7) ** 0.5
    assert m == pytest.approx(mean / sd)
    assert m < 2.0  # a properly varied week never trips


# ═══════════════════════════════════════════════════════════════════════════
# C — deload advance on the stored plan (app-level, pinned today)
# ═══════════════════════════════════════════════════════════════════════════

def _mk_session(d: date, session_type: str, dur: int, tss: int) -> dict:
    return {"day": d.isoformat(), "day_name": d.strftime("%a"),
            "session_type": session_type, "duration_min": dur,
            "tss_estimate": tss, "description": f"{session_type} {dur}min",
            "zwo_file": "", "zwo_name": "", "status": "pending"}


_WEEK_SHAPE = [("rest", 0, 0), ("vo2max", 60, 75), ("z2", 60, 45),
               ("threshold", 60, 70), ("rest", 0, 0), ("long_z2", 120, 90),
               ("recovery", 45, 25)]


def _mk_week(week_num: int, monday: date, is_stepback=False,
             tss_target=400) -> dict:
    sessions = [_mk_session(monday + timedelta(days=i), st, dur, tss)
                for i, (st, dur, tss) in enumerate(_WEEK_SHAPE)]
    return {"week_num": week_num, "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "phase": "continuous", "tss_target": tss_target,
            "is_stepback": is_stepback, "sessions": sessions}


def _mk_continuous_plan(today: date, flags=(False, False, False, True),
                        first_week_num=1, weeks_back=0) -> dict:
    """Continuous plan whose week[weeks_back] contains `today`."""
    monday = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks_back)
    return {
        "goal": {"type": "continuous", "focus": "both", "hours_per_week": 8.0,
                 "rest_days": [0, 4], "available_days": [1, 2, 3, 5, 6],
                 "distribution": "polarized"},
        "phases": [{"name": "continuous", "weeks": len(flags),
                    "start": monday.isoformat(),
                    "end": (monday + timedelta(weeks=len(flags), days=-1)).isoformat(),
                    "weekly_tss": 400, "focus": "rolling"}],
        "weeks": [_mk_week(first_week_num + i, monday + timedelta(weeks=i),
                           is_stepback=f) for i, f in enumerate(flags)],
        "generated": "2026-06-01T00:00:00",
    }


def _monotone_rides(today: date, tss=70, n=7) -> list[dict]:
    """n identical daily loads ending today → Foster monotony = cap (>= 2)."""
    return [{"started_at": f"{(today - timedelta(days=i)).isoformat()}T10:00:00",
             "tss": tss, "time_in_zone": {"z1": 600, "z2": 2400}}
            for i in range(n)]


def _calm_rides(today: date) -> list[dict]:
    """Varied light week → monotony well under 2.0, no ACWR signal."""
    return [{"started_at": f"{(today - timedelta(days=i)).isoformat()}T10:00:00",
             "tss": t, "time_in_zone": {"z1": 300, "z2": 1800}}
            for i, t in [(1, 90), (3, 30), (5, 60)]]


class DeloadAdvanceBase(unittest.TestCase):
    def setUp(self):
        app_module.clear_cache()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp
        self.today = date.today()

    def tearDown(self):
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def _write(self, plan: dict) -> Path:
        p = self._tmp / "current_plan.json"
        p.write_text(json.dumps(plan))
        return p

    def _advance(self, plan, rides):
        path = self._write(plan)
        with patch.object(app_module, "_load_all_rides_safe", return_value=rides):
            chip = app_module._maybe_advance_continuous_deload(
                plan, path, today=self.today)
        return chip, path


class TestDeloadAdvance(DeloadAdvanceBase):
    def test_monotony_trip_converts_current_week(self):
        plan = _mk_continuous_plan(self.today)
        original = json.loads(json.dumps(plan["weeks"][0]))
        chip, path = self._advance(plan, _monotone_rides(self.today))

        self.assertIsNotNone(chip, "monotony >= 2.0 must advance the deload")
        self.assertEqual(chip["trigger"]["trigger"], "monotony")
        self.assertGreaterEqual(chip["trigger"]["value"], 2.0)
        self.assertEqual(chip["week_num"], 1)
        self.assertFalse(chip["reverted"])
        self.assertTrue(chip["reason"])
        self.assertNotIn("original_week", chip, "chip payload stays lean")

        saved = json.loads(path.read_text())
        wk = saved["weeks"][0]
        self.assertTrue(wk["is_stepback"])
        # ×0.72 Issurin unload band via plan_week
        self.assertEqual(wk["tss_target"], round(400 * 0.72))
        today_iso = self.today.isoformat()
        for s in wk["sessions"]:
            if s["day"] >= today_iso:
                self.assertNotIn(s["session_type"], HARD_TYPES,
                                 f"deload day {s['day']} still hard: {s['session_type']}")
        # Past days stay verbatim (frozen); rest days never gain a ride.
        by_day = {s["day"]: s for s in original["sessions"]}
        for s in wk["sessions"]:
            if s["day"] < today_iso:
                self.assertEqual(s["session_type"], by_day[s["day"]]["session_type"])
            if by_day[s["day"]]["session_type"] == "rest":
                self.assertEqual(s["session_type"], "rest")
        # Revertible record with the full snapshot persisted.
        rec = saved["deload_advance"]
        self.assertEqual(rec["week_num"], 1)
        self.assertEqual(rec["original_week"], original)
        self.assertEqual(rec["refit_days"], chip["refit_days"])

    def test_acwr_trip_converts_current_week(self):
        # Plan started last week; last week fully completed at 2× its target.
        plan = _mk_continuous_plan(self.today, flags=(False, False, False, True),
                                   weeks_back=1)
        plan["weeks"][0]["tss_target"] = 100
        prev_monday = date.fromisoformat(plan["weeks"][0]["start"])
        rides = [{"started_at": f"{(prev_monday + timedelta(days=i)).isoformat()}T09:00:00",
                  "tss": t, "time_in_zone": {"z2": 1800}}
                 for i, t in [(0, 100), (2, 60), (4, 40)]]
        chip, path = self._advance(plan, rides)
        self.assertIsNotNone(chip, "ACWR 2.0 > 1.5 must advance the deload")
        self.assertEqual(chip["trigger"]["trigger"], "acwr")
        self.assertEqual(chip["week_num"], 2)
        saved = json.loads(path.read_text())
        self.assertTrue(saved["weeks"][1]["is_stepback"])
        self.assertFalse(saved["weeks"][0]["is_stepback"],
                         "the completed week is history — never rewritten")

    def test_calm_series_never_trips(self):
        plan = _mk_continuous_plan(self.today)
        before = json.dumps(plan["weeks"])
        chip, path = self._advance(plan, _calm_rides(self.today))
        self.assertIsNone(chip)
        self.assertEqual(json.dumps(json.loads(path.read_text())["weeks"]), before,
                         "no trip → plan untouched")

    def test_never_two_deloads_back_to_back(self):
        rides = _monotone_rides(self.today)
        # (a) current week already a deload → no-op
        plan = _mk_continuous_plan(self.today, flags=(True, False, False, False))
        chip, _ = self._advance(plan, rides)
        self.assertIsNone(chip)
        # (b) the week just ridden was the deload → no-op
        plan = _mk_continuous_plan(self.today, flags=(True, False, False, False),
                                   weeks_back=1)
        chip, _ = self._advance(plan, rides)
        self.assertIsNone(chip)
        # (c) NEXT week is the scheduled deload (relief ≤7d out) → no-op
        plan = _mk_continuous_plan(self.today, flags=(False, True, False, False))
        chip, _ = self._advance(plan, rides)
        self.assertIsNone(chip)

    def test_advance_latches_idempotent(self):
        plan = _mk_continuous_plan(self.today)
        rides = _monotone_rides(self.today)
        chip1, path = self._advance(plan, rides)
        self.assertIsNotNone(chip1)
        saved1 = path.read_text()
        # Second check: same chip surfaces, NOTHING is rewritten.
        plan2 = json.loads(saved1)
        with patch.object(app_module, "_load_all_rides_safe", return_value=rides):
            chip2 = app_module._maybe_advance_continuous_deload(
                plan2, path, today=self.today)
        self.assertEqual(chip2, chip1)
        self.assertEqual(path.read_text(), saved1)

    def test_finite_goal_paths_untouched(self):
        # The check is only ever invoked for continuous plans; even called
        # bare on a finite plan dict it must not write (guard belt).
        plan = _mk_continuous_plan(self.today)
        plan["goal"]["type"] = "general"
        path = self._write(plan)
        with patch.object(app_module, "_load_all_rides_safe",
                          return_value=_monotone_rides(self.today)):
            self.assertFalse(app_module._plan_is_continuous(plan))


class TestDeloadRevert(DeloadAdvanceBase):
    def _advanced_plan(self):
        plan = _mk_continuous_plan(self.today)
        original = json.loads(json.dumps(plan["weeks"][0]))
        chip, path = self._advance(plan, _monotone_rides(self.today))
        assert chip is not None
        return original, path

    def test_revert_restores_remaining_days_and_latches(self):
        original, path = self._advanced_plan()
        client = TestClient(app_module.app)
        r = client.post("/api/plan/continuous/deload-revert")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["reverted"])

        saved = json.loads(path.read_text())
        wk = saved["weeks"][0]
        self.assertFalse(wk["is_stepback"])
        self.assertEqual(wk["tss_target"], original["tss_target"])
        by_day = {s["day"]: s for s in original["sessions"]}
        today_iso = self.today.isoformat()
        for s in wk["sessions"]:
            if s["day"] >= today_iso:
                self.assertEqual(s, by_day[s["day"]],
                                 "remaining days must restore the snapshot")
        rec = saved["deload_advance"]
        self.assertTrue(rec["reverted"])

        # Latch: the same week can NOT re-trigger after a revert.
        with patch.object(app_module, "_load_all_rides_safe",
                          return_value=_monotone_rides(self.today)):
            chip = app_module._maybe_advance_continuous_deload(
                saved, path, today=self.today)
        self.assertIsNone(chip)

        # Second revert is a clean no-op.
        r2 = client.post("/api/plan/continuous/deload-revert")
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.json()["reverted"])

    def test_revert_without_advance_is_noop(self):
        self._write(_mk_continuous_plan(self.today))
        r = TestClient(app_module.app).post("/api/plan/continuous/deload-revert")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["reverted"])
        self.assertEqual(r.json()["reason"], "nothing_to_revert")


# ═══════════════════════════════════════════════════════════════════════════
# D — /api/today-session exposure (additive for continuous, absent for finite)
# ═══════════════════════════════════════════════════════════════════════════

class TodaySessionBase(unittest.TestCase):
    SLEEP = {"red_hrv_streak": 0, "ln_rmssd_7d": 3.90, "swc_lower": 3.70,
             "swc_upper": 4.10, "sleep_h": 7.5, "rhr_delta": 0}
    TRAINING = {"ctl": 50, "atl": 45, "tsb": 5}

    def setUp(self):
        app_module.clear_cache()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp
        self.today = date.today()
        self._patches = [
            patch.object(app_module, "_maybe_lazy_icu_sync", return_value=None),
            patch.object(app_module.db, "query_activities", return_value=[]),
            patch.object(app_module, "get_sleep_metrics",
                         return_value=dict(self.SLEEP)),
            patch.object(app_module, "get_today_metrics",
                         return_value=dict(self.TRAINING)),
        ]
        for p in self._patches:
            p.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def _get(self, rides):
        with patch.object(app_module, "_load_all_rides_safe", return_value=rides):
            r = self.client.get("/api/today-session")
        assert r.status_code == 200, r.text
        return r.json()


class TestTodaySessionContinuousFields(TodaySessionBase):
    def test_continuous_goal_carries_suggestion(self):
        plan = _mk_continuous_plan(self.today)
        (self._tmp / "current_plan.json").write_text(json.dumps(plan))
        data = self._get(_calm_rides(self.today))
        self.assertIn("continuous_suggestion", data)
        sug = data["continuous_suggestion"]
        self.assertEqual(set(sug), {"family", "reason"})
        self.assertIn(sug["family"], cpol.FAMILIES)
        self.assertTrue(sug["reason"])
        # calm rides → no deload chip
        self.assertNotIn("deload_advance", data)
        # existing shape intact (no breaking change)
        self.assertIn("planned", data)
        self.assertIn("adjusted", data)

    def test_finite_goal_has_no_continuous_fields(self):
        plan = _mk_continuous_plan(self.today)
        plan["goal"] = {"type": "general", "hours_per_week": 8.0,
                        "rest_days": [0, 4]}
        (self._tmp / "current_plan.json").write_text(json.dumps(plan))
        data = self._get(_calm_rides(self.today))
        self.assertNotIn("continuous_suggestion", data)
        self.assertNotIn("deload_advance", data)

    def test_monotony_trip_surfaces_deload_chip(self):
        plan = _mk_continuous_plan(self.today)
        path = self._tmp / "current_plan.json"
        path.write_text(json.dumps(plan))
        data = self._get(_monotone_rides(self.today))
        self.assertIn("deload_advance", data)
        chip = data["deload_advance"]
        self.assertEqual(chip["trigger"]["trigger"], "monotony")
        self.assertFalse(chip["reverted"])
        self.assertTrue(chip["reason"])
        # ... and the stored plan was actually converted.
        saved = json.loads(path.read_text())
        self.assertTrue(saved["weeks"][0]["is_stepback"])
        self.assertEqual(saved["deload_advance"]["week_num"], chip["week_num"])


# ═══════════════════════════════════════════════════════════════════════════
# focus plumbing (W2 API plumbing for the W1 engine field)
# ═══════════════════════════════════════════════════════════════════════════

def test_goal_from_plan_dict_carries_focus():
    g = app_module._goal_from_plan_dict(
        {"type": "continuous", "focus": "vo2", "hours_per_week": 8.0})
    assert g.goal_type == "continuous" and g.focus == "vo2"
    # default + finite goals stay harmless
    g2 = app_module._goal_from_plan_dict({"type": "general"})
    assert g2.focus == "both"
