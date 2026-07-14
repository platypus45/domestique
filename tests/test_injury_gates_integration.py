"""v4.6.6 WAVE-4-FIX — End-to-end integration tests for injury-prevention gates.

These tests go through the actual FastAPI endpoints (not direct calls to
adjust_today_session / reforecast). They exist because Wave 3 QA found the
existing tests stub the planner directly, missing the API plumbing bugs:

  CRITICAL-2: /api/today-session never passed rides_recent → G2/G7 dead.
  CRITICAL-3: /api/plan/reforecast never passed recent_activities → G3/G4 dead.
  CRITICAL-4: PlannedWeek mutations (tss_target, hit_per_week) never written
              back to current_plan.json after reforecast().
  CRITICAL-5: G3 dropped days were appended to g3_dropped_days but persistence
              only checked touched_days → G3 ran in memory then evaporated.

Each test exercises a complete request → mutation → re-read cycle so it would
catch a future regression that re-introduces any of the above plumbing bugs.

Citations (each test pins one row of MASTER_DECISIONS_v466 §1):
  G2 — Hulin 2014 *Br J Sports Med* 48:708-712 (48h Z5+ ceiling)
  G4 — Gabbett 2016 *Br J Sports Med* 50:273-280 (ACWR sweet-spot 0.8-1.3)
  G6 — Hooper & Mackinnon 1995 *J Sci Med Sport* (wellness composite ≥18)
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


def _today_iso() -> str:
    return date.today().isoformat()


def _ride(
    *,
    ride_id: str,
    started_at: datetime,
    tss: float,
    z5_secs: int = 0,
    z6_secs: int = 0,
    z7_secs: int = 0,
    feel: int | None = None,
    perceived_exertion: int | None = None,
) -> dict:
    """Synthesize a normalized ride dict that load_all_rides() would return.

    Matches the §3 shape persisted by ride_storage._normalize_icu_activity:
    `started_at` is the canonical timestamp; `time_in_zone` carries z1..z7
    seconds; `feel` and `perceived_exertion` are the v4.6.6 IMPL-C RPE fields.
    """
    iso = started_at.isoformat()
    return {
        "ride_id": ride_id,
        "source": "icu",
        "external_id": ride_id.split("_", 1)[-1],
        "started_at": iso,
        # Both fields surfaced so app.py + planner helpers all see a date.
        "date": iso[:10],
        "start_date_local": iso,
        "tss": tss,
        "duration_s": z5_secs + z6_secs + z7_secs + 1800,
        "time_in_zone": {
            "z1": 0, "z2": 1800, "z3": 0, "z4": 0,
            "z5": z5_secs, "z6": z6_secs, "z7": z7_secs,
        },
        "feel": feel,
        "perceived_exertion": perceived_exertion,
    }


def _stub_today_session_dependencies(revert_flag_dir: Path):
    """Patch out the readiness/sleep/training network calls used by
    /api/today-session so the test stays deterministic.

    ``revert_flag_dir``: tmp dir for the C6 revert flag file (3.4.2 M7 —
    the flag path defaults to the LIVE DATA_DIR, so one "Keep original"
    click on the dev machine suppressed every adjustment and turned the
    G2/G6 tests red; a nonexistent tmp file deterministically means
    "not reverted today").

    Returns a list of started patchers — caller stops them in tearDown.
    """
    patchers = [
        patch.object(app_module, "_maybe_lazy_icu_sync", return_value=None),
        patch.object(
            app_module, "_readiness_revert_flag_path",
            return_value=revert_flag_dir / "readiness_cap_reverted.json",
        ),
        patch.object(
            app_module, "get_sleep_metrics",
            return_value={"ln_rmssd_7d": 4.0, "swc_lower": 3.5,
                          "swc_upper": 4.5, "sleep_h": 7.5,
                          "rhr_delta": 0.0, "red_hrv_streak": 0},
        ),
        patch.object(
            app_module, "get_today_metrics",
            return_value={"tsb": 5.0, "ctl": 50.0, "atl": 45.0},
        ),
        patch.object(
            app_module, "_recent_dfa_and_decoupling",
            # v1.8.16 — 4-tuple: (dfa_vals, last_dec, last_dec_date, newest_dfa_date).
            return_value=([1.0, 1.0], None, None, None),
        ),
        # Force readiness "fresh" so any downshift comes only from injury gates.
        patch.object(
            app_module, "compute_readiness",
            return_value={"score": 90, "dfa_cap": {"cap_applied": False}},
        ),
        patch.object(
            app_module, "_readiness_with_data_status",
            side_effect=lambda r, has_local_load=False: {**r, "data_status": "ok"},
        ),
        patch.object(
            app_module, "_merge_training_load",
            return_value={"ctl": 50.0, "atl": 45.0, "tsb": 5.0},
        ),
        # No yesterday-was-hard signal — keeps G1 quiet so we isolate G2/G6.
        patch.object(app_module.db, "query_activities", return_value=[]),
    ]
    for p in patchers:
        p.start()
    return patchers


class _IntegrationBase(unittest.TestCase):
    """Shared setUp for the three integration tests: temp DB + temp PLAN_DIR
    pointed at a synth plan, all upstream HRV/sleep/RHR calls stubbed.
    """

    def setUp(self):
        # 3.4.2 M7 — drop the shared 5-min memo store ("sleep", "training",
        # "recent_dfa_decoupling", ...) so values cached by an EARLIER test
        # (or from the live environment) can never bypass this suite's
        # patched get_sleep_metrics/get_today_metrics in the full gate.
        app_module.clear_cache()

        # Temp sqlite db — isolate from production data.
        self._tmpdb = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdb.name) / "integration.sqlite"
        import db as db_module
        self._original_db_path = db_module.DB_PATH
        db_module.close_all_connections()
        db_module.set_db_path(self._db_path)
        db_module.init_db()
        self._db_module = db_module

        # Temp plan dir — write a vo2max-today plan so adjust_today_session
        # has a HIT prescription to potentially downshift.
        self._tmpplan = tempfile.TemporaryDirectory()
        self._plan_path = Path(self._tmpplan.name)
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._plan_path
        self._write_synth_plan()

        # Stub upstream calls (C6 revert flag sandboxed into the tmp plan dir).
        self._patchers = _stub_today_session_dependencies(self._plan_path)

        self.client = TestClient(app_module.app)

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpplan.cleanup()
        self._db_module.close_all_connections()
        self._db_module.set_db_path(self._original_db_path)
        self._tmpdb.cleanup()

    def _write_synth_plan(self) -> None:
        """A 3-week build1 plan: last week (completed, 200 TSS planned),
        this week (planned 400 TSS; today=vo2max), next week (planned 420 TSS,
        the G4 scale target). 100 TSS daily-avg keeps yesterday's ratio low.
        """
        today = date.today()
        last_mon = today - timedelta(days=today.weekday() + 7)
        this_mon = last_mon + timedelta(days=7)
        next_mon = this_mon + timedelta(days=7)

        def _wk(start: date, *, tss_target: float, today_session: bool) -> dict:
            sessions = []
            for i in range(7):
                d = start + timedelta(days=i)
                if today_session and d == today:
                    sessions.append({
                        "day": d.isoformat(),
                        "day_name": d.strftime("%A"),
                        "session_type": "vo2max",
                        "duration_min": 60,
                        "tss_estimate": 90,
                        "description": "VO2max 5x4min",
                        "status": "pending",
                    })
                else:
                    sessions.append({
                        "day": d.isoformat(),
                        "day_name": d.strftime("%A"),
                        "session_type": "rest",
                        "duration_min": 0,
                        "tss_estimate": 0,
                        "description": "",
                        "status": "pending",
                    })
            return {
                "week_num": 0,  # filled below
                "start": start.isoformat(),
                "end": (start + timedelta(days=6)).isoformat(),
                "phase": "build1",
                "tss_target": tss_target,
                "is_stepback": False,
                "hit_per_week": 3,
                "sessions": sessions,
            }

        last_wk = _wk(last_mon, tss_target=200.0, today_session=False)
        last_wk["week_num"] = 1
        this_wk = _wk(this_mon, tss_target=400.0, today_session=True)
        this_wk["week_num"] = 2
        next_wk = _wk(next_mon, tss_target=420.0, today_session=False)
        next_wk["week_num"] = 3

        plan = {
            "goal": {"type": "general", "hours_per_week": 8.0,
                     "rest_days": [0], "available_days": [1, 2, 3, 4, 5, 6]},
            "weeks": [last_wk, this_wk, next_wk],
            "phases": [{"name": "build1", "start": last_mon.isoformat(),
                        "end": (next_mon + timedelta(days=6)).isoformat(),
                        "weeks": 3, "focus": "build", "weekly_tss": 400,
                        "hit_per_week": 3,
                        "session_types": ["z2", "vo2max", "threshold"]}],
            "generated": datetime.now().isoformat(),
        }
        (self._plan_path / "current_plan.json").write_text(json.dumps(plan))


class TestG6IntegrationViaDailyLog(_IntegrationBase):
    """CRITICAL-1 + CRITICAL-2 net: real /api/daily-log POST + /api/today-session.

    Hooper & Mackinnon 1995 — wellness composite >=18 should cap today's HIT.
    The polarity bug: planner used `8 - sleep_quality` while db.py used direct
    sum, so (sleep=7, fat=3, str=4, sor=4) computed 12 in planner / 18 in db.
    Test asserts both halves see 18 and the gate fires.
    """

    def test_post_daily_log_then_today_session_fires_g6(self):
        # Step 1: morning leg-check submits a tuple where direct-sum = 18.
        # The diagnostic case from QA: well-slept (sleep=7 means terrible per
        # the form's 1=best,7=worst) but high stress + soreness; pre-fix the
        # planner inverted sleep_quality and saw hooper=12 → no fire.
        post_body = {"sleep_quality": 7, "fatigue": 3, "stress": 4, "soreness": 4}
        r = self.client.post("/api/daily-log", json=post_body)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(body["entry"]["hooper_index"], 18,
                         "db must store direct-sum hooper_index")

        # Step 2: GET /api/today-session — G6 should fire and force Z2.
        with patch.object(app_module, "_load_all_rides_safe", return_value=[]):
            ts = self.client.get("/api/today-session")
        self.assertEqual(ts.status_code, 200, ts.text)
        out = ts.json()
        adj = out.get("adjusted") or {}
        self.assertEqual(
            adj.get("session_type"), "z2",
            f"G6 should cap vo2max → z2 when hooper=18 (got "
            f"{adj.get('session_type')!r}; reason={out.get('reason')!r})"
        )
        # The reason must mention G6 + the actual hooper value.
        reason = out.get("reason") or ""
        self.assertIn("G6", reason, f"reason missing G6 marker: {reason!r}")
        self.assertIn("18", reason,
                      f"reason should cite hooper=18 (got {reason!r})")


class TestG2IntegrationViaTodaySession(_IntegrationBase):
    """CRITICAL-2 net: /api/today-session must pass rides to the planner so
    G2 (Hulin 2014 — 48h Z5+ ≥ 25min) actually fires from the production code
    path. Pre-fix the helpers received an empty list and the gate was dead.
    """

    def test_two_z5_rides_force_z2_today(self):
        # Two recent rides each with 15 minutes in Z5 → 30 min total in 48h
        # → exceeds the 25-min ceiling. Each ride is timestamped within the
        # 48-hour window the helper uses.
        now = datetime.now(timezone.utc).astimezone()
        rides = [
            _ride(
                ride_id="icu_z5_r1",
                started_at=now - timedelta(hours=12),
                tss=80.0,
                z5_secs=900,  # 15 min
            ),
            _ride(
                ride_id="icu_z5_r2",
                started_at=now - timedelta(hours=36),
                tss=70.0,
                z5_secs=900,  # 15 min → total 30 min Z5+
            ),
        ]

        # No daily_log → G6 silent. Empty activity list → G1 silent.
        with patch.object(app_module, "_load_all_rides_safe", return_value=rides):
            r = self.client.get("/api/today-session")
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        adj = out.get("adjusted") or {}
        self.assertEqual(
            adj.get("session_type"), "z2",
            f"G2 should force vo2max → z2 with 30min Z5+ in last 48h "
            f"(got {adj.get('session_type')!r}; reason={out.get('reason')!r})"
        )
        reason = out.get("reason") or ""
        self.assertIn("G2", reason, f"reason missing G2 marker: {reason!r}")


class TestG4IntegrationPersistsTssTarget(_IntegrationBase):
    """CRITICAL-3 + CRITICAL-4 net: /api/plan/reforecast must pass
    recent_activities (Gabbett 2016 ACWR) AND persist the resulting
    tss_target / hit_per_week / auto_acwr_scaled mutations to disk.

    Pre-fix the endpoint computed acwr_ratio against an empty list (gate
    skipped) AND only wrote per-day session changes (never the week-level
    fields). Both bugs are caught here by reading current_plan.json after
    the request and asserting the next week's tss_target dropped.
    """

    def test_overload_week_scales_next_week_tss_target(self):
        # The plan has last week tss_target=200 (completed). Inject 400 TSS
        # of activities into that week → actual/planned = 2.0 > 1.5, G4 fires.
        today = date.today()
        last_mon = today - timedelta(days=today.weekday() + 7)
        last_sun = last_mon + timedelta(days=6)

        synth_acts = []
        # 4 rides of 100 TSS each across the previous week.
        for i in range(4):
            d = last_mon + timedelta(days=i)
            synth_acts.append({
                "date": d.isoformat(),
                "tss": 100.0,
                "duration_sec": 3600,
                "type": "ride",
                "icu_training_load": 100,
            })

        # Verify pre-state: next week's tss_target == 420.
        plan_before = json.loads(
            (self._plan_path / "current_plan.json").read_text()
        )
        next_wk_before = next(
            w for w in plan_before["weeks"] if w["week_num"] == 3
        )
        self.assertEqual(next_wk_before["tss_target"], 420.0)
        self.assertNotIn("auto_acwr_scaled", next_wk_before)
        next_wk_hit_before = next_wk_before.get("hit_per_week", 3)

        with patch.object(
            app_module.db, "query_activities", return_value=synth_acts,
        ):
            r = self.client.post("/api/plan/reforecast")

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        rf = body.get("reforecast") or {}
        # acwr_ratio = 400/200 = 2.0; gate fires → scales week 3.
        self.assertGreater(
            rf.get("acwr_ratio", 0), 1.5,
            f"acwr_ratio must exceed 1.5 (got {rf.get('acwr_ratio')!r})"
        )
        self.assertEqual(
            rf.get("acwr_scaled_week"), 3,
            f"G4 should scale week 3 (next non-stepback week); "
            f"got {rf.get('acwr_scaled_week')!r}"
        )

        # Re-read disk and assert the mutation was persisted.
        plan_after = json.loads(
            (self._plan_path / "current_plan.json").read_text()
        )
        next_wk_after = next(
            w for w in plan_after["weeks"] if w["week_num"] == 3
        )
        # tss_target should be 420 * 0.85 = 357.
        self.assertAlmostEqual(
            next_wk_after["tss_target"], 420.0 * 0.85, places=1,
            msg=f"tss_target was not persisted; got "
                f"{next_wk_after['tss_target']!r} (expected ~357)"
        )
        self.assertEqual(
            next_wk_after.get("hit_per_week"), next_wk_hit_before - 1,
            f"hit_per_week should be decremented; "
            f"got {next_wk_after.get('hit_per_week')!r}"
        )
        self.assertTrue(
            next_wk_after.get("auto_acwr_scaled"),
            f"auto_acwr_scaled flag missing on disk: {next_wk_after!r}"
        )


if __name__ == "__main__":
    unittest.main()
