"""H1 regression (v3.1.0 evaluator) — /api/plan/generate with the REAL UI
payload shape: the form's plan-weeks slider is TODAY-anchored, so a backdated
event plan must have its week budget recomputed server-side from the
start_date→event span. Pre-fix: weeks_available() short-circuited on the
stale plan_weeks=12 and the fill-to-taper stretch dumped the 4-week error
into a 7-week PEAK block."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class TestBackdatedGenerateRecomputesWeeks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="entry_api_"))
        self._patch = patch.object(app_module, "_plan_dir", return_value=self.tmp)
        self._patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch.stop()

    def test_ui_stale_weeks_overridden_by_full_runway(self):
        today = date.today()
        start = today - timedelta(days=28)     # 4 weeks in
        event = today + timedelta(days=84)     # 12 more weeks to race day
        r = self.client.post("/api/plan/generate", json={
            "goal": "event",
            "event_date": event.isoformat(),
            "start_date": start.isoformat(),
            "entry_mode": "declared",
            "weeks": 12,                       # the stale TODAY-anchored slider value
            "hours_per_week": 8.0,
            "event_km": 150, "event_climb": 1500, "event_type": "granfondo",
        })
        self.assertEqual(r.status_code, 200, r.text)

        plan = json.loads((self.tmp / "current_plan.json").read_text())
        weeks = plan["weeks"]
        # Full ~16-week runway (16/17 rows by weekday alignment), anchored on
        # the backdated start; the H1 bug produced the UI's TODAY-anchored 12.
        self.assertEqual(weeks[0]["start"], start.isoformat())
        self.assertGreaterEqual(len(weeks), 15,
                                f"stale plan_weeks won (H1): {len(weeks)} weeks")
        self.assertLessEqual(len(weeks), 17)
        self.assertEqual(plan["goal"].get("start_date"), start.isoformat())
        # H1 signature was a stretched multi-week peak: cap consecutive peaks.
        phases = [w.get("phase") for w in weeks]
        longest_peak = cur = 0
        for p in phases:
            cur = cur + 1 if p == "peak" else 0
            longest_peak = max(longest_peak, cur)
        self.assertLessEqual(longest_peak, 3,
                             f"stretched peak block (H1): {phases}")
        # Elapsed rows sessionless; nothing scheduled before today.
        for w in weeks:
            if w["end"] < today.isoformat():
                self.assertEqual(w["sessions"], [])
            for s in w.get("sessions", []):
                self.assertGreaterEqual(s["day"], today.isoformat())


class TestPhaseEditorApi(unittest.TestCase):
    """Phase-split editor (v3.2.0) — /api/plan/preview + /api/plan/generate.

    Parity scope (A8): LENGTHS + DATES only — the preview's thin Goal lacks
    rtss/athlete, so per-phase TSS may diverge; it is deliberately excluded.
    Real-today dates (unpinned — mirrors the H1 class above)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="phase_editor_api_"))
        self._patch = patch.object(app_module, "_plan_dir", return_value=self.tmp)
        self._patch.start()
        self.client = TestClient(app_module.app)
        self.event = (date.today() + timedelta(days=112)).isoformat()  # 16w
        self.custom = {"base": 5, "build1": 3, "build2": 3, "peak": 2,
                       "taper": 3}

    def tearDown(self):
        self._patch.stop()

    def _preview(self, **extra):
        params = {"goal": "event", "event_date": self.event, **extra}
        r = self.client.get("/api/plan/preview", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ── parse helper: dict POST ≡ URL-encoded JSON GET ───────────────────────

    def test_parse_helper_get_post_equivalence(self):
        as_dict, err_d = app_module._parse_phase_weeks({"base": 3, "peak": 1})
        as_str, err_s = app_module._parse_phase_weeks('{"base": 3, "peak": 1}')
        self.assertEqual((as_dict, err_d), (as_str, err_s))
        self.assertEqual(as_dict, {"base": 3, "peak": 1})
        self.assertEqual(app_module._parse_phase_weeks(None), (None, ""))
        self.assertEqual(app_module._parse_phase_weeks(""), (None, ""))
        self.assertEqual(app_module._parse_phase_weeks({}), (None, ""))
        vec, reason = app_module._parse_phase_weeks("{not json")
        self.assertIsNone(vec)
        self.assertIn("not valid JSON", reason)
        vec, reason = app_module._parse_phase_weeks("[1,2]")
        self.assertIsNone(vec)
        self.assertIn("object", reason)

    # ── preview: rec echo / applied / fallback / disabled ────────────────────

    def test_preview_echoes_recommendation_vector_and_m(self):
        d = self._preview()
        self.assertEqual(d["plan_weeks"], 16)              # M — server-side
        self.assertIsNone(d["phase_weeks_status"])
        self.assertEqual(sum(d["phase_weeks_rec"].values()), 16)
        self.assertIn("taper", d["phase_weeks_rec"])

    def test_preview_nonevent_rec_sums_to_m_minus_one(self):
        r = self.client.get("/api/plan/preview",
                            params={"goal": "general", "plan_weeks": 12})
        d = r.json()
        self.assertEqual(d["plan_weeks"], 12)
        self.assertEqual(sum(d["phase_weeks_rec"].values()), 11)
        self.assertNotIn("consolidation", d["phase_weeks_rec"])

    def test_preview_applies_valid_custom(self):
        d = self._preview(phase_weeks=json.dumps(self.custom))
        self.assertEqual(d["phase_weeks_status"], "applied")
        by_name = {p["name"]: p for p in d["phases"]}
        for name in ("base", "build1", "build2", "taper"):
            self.assertEqual(by_name[name]["weeks"], self.custom[name], name)

    def test_preview_invalid_falls_back_with_reason(self):
        base = self._preview()
        bad = dict(self.custom, base=9)                    # sum 20 ≠ 16
        d = self._preview(phase_weeks=json.dumps(bad))
        self.assertTrue(str(d["phase_weeks_status"]).startswith(
            "fallback:split totals 20"))
        self.assertEqual(d["phases"], base["phases"])      # recommendation

    def test_preview_unparseable_falls_back_no_500(self):
        d = self._preview(phase_weeks="{broken")
        self.assertTrue(str(d["phase_weeks_status"]).startswith("fallback:"))
        self.assertTrue(d["phases"])

    def test_preview_micro_plan_editor_disabled(self):
        soon = (date.today() + timedelta(days=10)).isoformat()
        d = self._preview(event_date=soon,
                          phase_weeks=json.dumps({"taper": 1}))
        self.assertIsNone(d["phase_weeks_rec"])
        self.assertIn("race-week", d["phase_weeks_disabled_reason"])
        self.assertTrue(str(d["phase_weeks_status"]).startswith("fallback:"))

    def test_preview_short_runway_editor_disabled_high1(self):
        # Evaluator HIGH-1: 14-27d window — M floors to 4, real weeks 2-3.
        soon = (date.today() + timedelta(days=16)).isoformat()
        d = self._preview(event_date=soon,
                          phase_weeks=json.dumps(
                              {"build1": 2, "peak": 1, "taper": 1}))
        self.assertIsNone(d["phase_weeks_rec"])
        self.assertIn("under four weeks", d["phase_weeks_disabled_reason"])
        self.assertTrue(str(d["phase_weeks_status"]).startswith("fallback:"))

    def test_preview_short_runway_backdated_keeps_editor(self):
        soon = (date.today() + timedelta(days=16)).isoformat()
        back = (date.today() - timedelta(days=42)).isoformat()
        d = self._preview(event_date=soon, start_date=back)
        self.assertIsNotNone(d["phase_weeks_rec"])

    def test_generate_nonfinite_never_persisted_high2(self):
        # Evaluator HIGH-2: bare Infinity parses via request.json(); it must
        # die at parse — never reach current_plan.json (stored DoS: every
        # later GET /api/plan 500s on allow_nan=False serialization).
        r = self.client.post(
            "/api/plan/generate",
            content=json.dumps({
                "goal": "event", "event_date": self.event, "weeks": 16,
                "hours_per_week": 8.0, "event_km": 150, "event_climb": 1500,
                "event_type": "granfondo",
                "phase_weeks": {"base": 1e999},   # serializes as Infinity
            }, allow_nan=True),
            headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 200, r.text)
        raw = (self.tmp / "current_plan.json").read_text()
        self.assertNotIn("Infinity", raw)
        self.assertNotIn("NaN", raw)
        plan = json.loads(raw)  # parses ⇒ finite
        self.assertIsNone(plan["goal"].get("phase_weeks"))
        self.assertTrue(str(plan["phase_weeks_status"]).startswith(
            "fallback:phase_weeks contains a non-finite number"))
        self.assertEqual(
            self.client.get("/api/plan").status_code, 200)

    def test_generate_nested_nonfinite_never_persisted_high2_round2(self):
        # Round 2: Infinity hidden one level down (dict / list value) must
        # die at parse too — the keep-as-sent persistence would otherwise
        # store it and brick every later plan load.
        for hostile in ({"base": {"x": 1e999}}, {"base": [1e999]},
                        {"base": {"deep": [{"deeper": float("nan")}]}}):
            r = self.client.post(
                "/api/plan/generate",
                content=json.dumps({
                    "goal": "event", "event_date": self.event, "weeks": 16,
                    "hours_per_week": 8.0, "event_km": 150,
                    "event_climb": 1500, "event_type": "granfondo",
                    "phase_weeks": hostile,
                }, allow_nan=True),
                headers={"content-type": "application/json"})
            self.assertEqual(r.status_code, 200, r.text)
            raw = (self.tmp / "current_plan.json").read_text()
            self.assertNotIn("Infinity", raw)
            self.assertNotIn("NaN", raw)
            plan = json.loads(raw)
            self.assertIsNone(plan["goal"].get("phase_weeks"))
            self.assertEqual(self.client.get("/api/plan").status_code, 200)

    # ── generate: persist + stamp + preview parity (lengths+dates) ───────────

    def _generate(self, phase_weeks):
        r = self.client.post("/api/plan/generate", json={
            "goal": "event", "event_date": self.event, "weeks": 16,
            "hours_per_week": 8.0, "event_km": 150, "event_climb": 1500,
            "event_type": "granfondo", "phase_weeks": phase_weeks,
        })
        self.assertEqual(r.status_code, 200, r.text)
        return json.loads((self.tmp / "current_plan.json").read_text())

    def test_generate_applies_persists_and_matches_preview(self):
        prev = self._preview(phase_weeks=json.dumps(self.custom))
        plan = self._generate(self.custom)
        self.assertEqual(plan["phase_weeks_status"], "applied")
        self.assertEqual(plan["goal"]["phase_weeks"], self.custom)
        # A8 parity: lengths + dates ONLY (never TSS).
        parity = lambda phases: [(p["name"], p["weeks"], p["start"], p["end"])
                                 for p in phases]
        self.assertEqual(parity(plan["phases"]), parity(prev["phases"]))

    def test_generate_invalid_falls_back_no_500(self):
        bad = dict(self.custom, taper=4)
        prev_rec = self._preview()
        plan = self._generate(bad)
        self.assertTrue(str(plan["phase_weeks_status"]).startswith(
            "fallback:taper is capped at 3"))
        # The user's numbers survive for form repopulation…
        self.assertEqual(plan["goal"]["phase_weeks"], bad)
        # …while the built phases are the recommendation (== rec preview).
        parity = lambda phases: [(p["name"], p["weeks"], p["start"], p["end"])
                                 for p in phases]
        self.assertEqual(parity(plan["phases"]), parity(prev_rec["phases"]))

    def test_generate_phantom_custom_stored_none_no_badge(self):
        rec = self._preview()["phase_weeks_rec"]           # A3
        plan = self._generate(rec)
        self.assertIsNone(plan["goal"]["phase_weeks"])
        self.assertNotIn("phase_weeks_status", plan)


class TestEntryScanEndpoint(unittest.TestCase):
    """MODE 2 — GET /api/plan/entry-scan (IP B-LOCKED-3): scan→propose is
    read-only; params mirror /api/plan/preview; missing goal/target params
    400 cleanly (not 422/500)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="entry_scan_"))
        self._plan_patch = patch.object(app_module, "_plan_dir",
                                        return_value=self.tmp)
        self._plan_patch.start()
        today = date.today()
        rides = []
        for w in range(1, 5):  # 4 whole compliant weeks back from today
            for days_back in (7 * w, 7 * w - 3):
                rides.append({
                    "started_at": (today - timedelta(days=days_back)).isoformat()
                                  + "T09:00:00",
                    "tss": None,                 # exercise the cascade …
                    "icu_training_load": 300.0,  # … icu_training_load fallback
                })
        self._rides_patch = patch.object(app_module, "_load_all_rides_safe",
                                         return_value=rides)
        self._rides_patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._rides_patch.stop()
        self._plan_patch.stop()

    def test_scan_shape_proposal_and_zero_writes(self):
        r = self.client.get("/api/plan/entry-scan",
                            params={"goal": "general", "plan_weeks": 12})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        # 3.3.3 (L4-UX 2): + the scan-result card fields (plan_weeks /
        # entry_week / plan_end_date / goal) — app-side date math on top of
        # what recognize_entry computed (tests/test_333_l4_ux.py pins their
        # values; this stays the canonical key-set pin).
        self.assertEqual(set(d.keys()), {"proposal_weeks",
                                         "equivalent_start_date", "capped",
                                         "weeks_remaining", "weeks",
                                         "plan_weeks", "entry_week",
                                         "plan_end_date", "goal"})
        self.assertEqual(d["proposal_weeks"], 4)
        self.assertEqual(d["equivalent_start_date"],
                         (date.today() - timedelta(days=28)).isoformat())
        self.assertTrue(d["capped"])
        self.assertEqual(len(d["weeks"]), 4)
        for row in d["weeks"]:
            self.assertEqual(set(row.keys()),
                             {"index", "window_start", "actual_tss",
                              "target_tss", "qualifies", "shape_note"})
            self.assertTrue(row["qualifies"])
        # Zero writes: the scan proposes, only Generate persists.
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_scan_event_goal_derives_runway_from_event_date(self):
        event = date.today() + timedelta(days=84)
        r = self.client.get("/api/plan/entry-scan",
                            params={"goal": "event",
                                    "event_date": event.isoformat()})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["proposal_weeks"], 4)

    def test_scan_400s_on_missing_goal_or_target_params(self):
        # No goal at all.
        self.assertEqual(self.client.get("/api/plan/entry-scan").status_code,
                         400)
        # Event goal without an event date.
        self.assertEqual(
            self.client.get("/api/plan/entry-scan",
                            params={"goal": "event"}).status_code, 400)
        # Non-event goal without any week budget or end date.
        self.assertEqual(
            self.client.get("/api/plan/entry-scan",
                            params={"goal": "general",
                                    "plan_weeks": 0}).status_code, 400)


class TestRecognizedEntryModePersistence(unittest.TestCase):
    """B-LOCKED-7: entry_mode="recognized" is provenance — it must survive
    generate → plan dict → _goal_from_plan_dict so regenerate reuses the
    stored anchor instead of silently re-running the recognizer."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="entry_recog_"))
        self._patch = patch.object(app_module, "_plan_dir",
                                   return_value=self.tmp)
        self._patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch.stop()

    def test_recognized_roundtrip_through_generate(self):
        start = date.today() - timedelta(days=14)
        r = self.client.post("/api/plan/generate", json={
            "goal": "general", "weeks": 10, "hours_per_week": 8.0,
            "start_date": start.isoformat(), "entry_mode": "recognized",
        })
        self.assertEqual(r.status_code, 200, r.text)
        plan = json.loads((self.tmp / "current_plan.json").read_text())
        self.assertEqual(plan["goal"].get("entry_mode"), "recognized")
        self.assertEqual(plan["goal"].get("start_date"), start.isoformat())
        goal = app_module._goal_from_plan_dict(plan["goal"])
        self.assertEqual(goal.entry_mode, "recognized")
        self.assertEqual(goal.start_date, start)


if __name__ == "__main__":
    unittest.main()
