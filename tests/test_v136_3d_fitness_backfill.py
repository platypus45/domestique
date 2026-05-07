"""v1.3.6 — 3D-fitness placeholder & backfill UX.

User report: the energy-system breakdown panel shows
``"3D fitness curves will populate after IMPL-3D-MODEL has computed
Banister components for the wellness window."`` indefinitely. The
placeholder is opaque — there is no signal what the user can do, what
N more rides means, or whether it's blocked.

Root cause: ``ride_storage.compute_ride_xss`` exists (since v1.0.6) but
is **not wired into any production ride-import path**. So
``ss_cp_daily`` / ``ss_w_prime_daily`` / ``ss_pmax_daily`` rows never
land in athlete_metrics, ``_augment_wellness_with_3d_fitness`` reads no
history, and every wellness record's ``cp_fitness/w_prime_fitness/
pmax_fitness`` stays None — placeholder fires forever.

Fix v1.3.6:
  1. Wire ``compute_ride_xss`` into ``_parse_fit_stats`` so future FIT
     imports populate SS aggregates.
  2. Add ``GET /api/wellness/3d-fitness-status`` and
     ``POST /api/wellness/backfill-3d-fitness`` so users can audit and
     run a one-shot backfill from the dashboard.
  3. Replace the placeholder with concrete numbers + a button.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


def _bootstrap_home(tmp: Path):
    base = tmp / ".domestique"
    base.mkdir(parents=True, exist_ok=True)
    p = base / "profiles" / "default"
    p.mkdir(parents=True, exist_ok=True)
    (p / "athlete.json").write_text(json.dumps({
        "ftp": 250, "weight_kg": 72.0, "lbm_kg": 58.0,
        "lthr": 175, "max_hr": 196,
        "wprime_j": 22000, "pmax_w": 1100,
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


def _install_fake_strain_score():
    fake = types.ModuleType("strain_score")

    def compute_xss_components(power_series, cp, wprime_j, pmax):
        if not power_series:
            return {}
        total = 0.0; cp_part = 0.0; wp_part = 0.0; pm_part = 0.0
        for p in power_series:
            pv = float(p or 0)
            if pv <= 0:
                continue
            ss = pv / max(cp, 1) * (100.0 / 3600.0)
            total += ss
            if pv <= cp:
                cp_part += ss
            else:
                wp_part += ss * 0.7
                pm_part += ss * 0.3
        return {
            "xss_total": round(total, 2),
            "xss_cp": round(cp_part, 2),
            "xss_w_prime": round(wp_part, 2),
            "xss_pmax": round(pm_part, 2),
        }

    def banister(series, tau_fit=42.0, tau_fat=7.0):
        # Trivial passthrough so _augment_wellness_with_3d_fitness can run.
        if not series:
            return (0.0, 0.0, 0.0)
        return (sum(series) / len(series), 0.0, 0.0)

    fake.compute_xss_components = compute_xss_components
    fake.banister = banister
    sys.modules["strain_score"] = fake


def _uninstall_fake_strain_score():
    sys.modules.pop("strain_score", None)


class _BackfillBase(unittest.TestCase):
    def setUp(self):
        import db
        from profile_manager import ProfileManager

        self.tmp = Path(tempfile.mkdtemp())
        _bootstrap_home(self.tmp)
        self._home_patch = patch("pathlib.Path.home", return_value=self.tmp)
        self._home_patch.start()
        ProfileManager._instance = None
        self.pm = ProfileManager.get()
        self.dbfile = self.tmp / "t.db"
        db.set_db_path(self.dbfile)
        db.close_all_connections()
        db.init_db()
        _install_fake_strain_score()

        # Patch ride storage dirs to tmp.
        import ride_storage as _rs
        self._icu_dir = self.tmp / "icu"
        self._icu_dir.mkdir(parents=True, exist_ok=True)
        self._fit_dir = self.tmp / "fit"
        self._fit_dir.mkdir(parents=True, exist_ok=True)
        self._patch_icu = patch.object(_rs, "_icu_rides_dir", return_value=self._icu_dir)
        self._patch_fit = patch.object(_rs, "_fit_rides_dir", return_value=self._fit_dir)
        self._patch_icu.start()
        self._patch_fit.start()

        import app as app_module
        self.client = TestClient(app_module.app)

    def tearDown(self):
        from profile_manager import ProfileManager
        self._patch_fit.stop()
        self._patch_icu.stop()
        _uninstall_fake_strain_score()
        self._home_patch.stop()
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_icu_ride(self, started_at: str, ext_id: str = "i_1"):
        """Drop a normalized ICU ride file under the patched _icu_rides_dir."""
        rec = {
            "ride_id": f"icu_{ext_id}",
            "external_id": ext_id,
            "source": "icu",
            "name": "test ride",
            "started_at": started_at,
            "duration_s": 3600,
        }
        (self._icu_dir / f"{ext_id}.json").write_text(json.dumps(rec))


class TestStatusEndpoint(_BackfillBase):
    """v1.3.6 — GET /api/wellness/3d-fitness-status reports K of M."""

    def test_status_endpoint_returns_zero_when_no_rides(self):
        r = self.client.get("/api/wellness/3d-fitness-status")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["rides_total_post_v106"], 0)
        self.assertEqual(body["rides_with_ss"], 0)

    def test_status_counts_post_v106_rides_only(self):
        # 2 post-v1.0.6, 1 pre.
        self._seed_icu_ride("2026-05-04T08:00:00", "post1")
        self._seed_icu_ride("2026-05-05T08:00:00", "post2")
        self._seed_icu_ride("2026-04-30T08:00:00", "pre1")
        r = self.client.get("/api/wellness/3d-fitness-status")
        body = r.json()
        self.assertEqual(body["rides_total_post_v106"], 2)
        self.assertEqual(body["rides_with_ss"], 0)


class TestBackfillWritesMetrics(_BackfillBase):
    """v1.3.6 — POST /api/wellness/backfill-3d-fitness lands SS rows."""

    def test_backfill_writes_ss_metrics_for_eligible_rides(self):
        # Seed an ICU ride and patch fetch_activity_streams to return power.
        self._seed_icu_ride("2026-05-05T08:00:00", "i_post")
        with patch("training.fetch_activity_streams", return_value={
            "watts": [200] * 1800 + [320] * 600,
        }):
            r = self.client.post("/api/wellness/backfill-3d-fitness")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertGreaterEqual(body["backfilled"], 1)

        # Verify athlete_metrics has the three SS rows for that day.
        import db
        conn = db.get_db()
        rows = {
            r["metric"]: r["value"]
            for r in conn.execute(
                "SELECT metric, value FROM athlete_metrics WHERE date = '2026-05-05'"
            ).fetchall()
        }
        self.assertIn("ss_cp_daily", rows)
        self.assertIn("ss_w_prime_daily", rows)
        self.assertIn("ss_pmax_daily", rows)
        for m, v in rows.items():
            self.assertGreater(v, 0)

    def test_backfill_skips_rides_with_existing_ss(self):
        # Pre-seed athlete_metrics row for the ride day.
        import db
        db.log_metric("2026-05-05", "ss_cp_daily", 12.3, source="computed")
        self._seed_icu_ride("2026-05-05T08:00:00", "i_post")
        with patch("training.fetch_activity_streams", return_value={
            "watts": [200] * 1800,
        }):
            r = self.client.post("/api/wellness/backfill-3d-fitness")
        body = r.json()
        # Already has SS data → counted as skipped, not backfilled.
        self.assertEqual(body["backfilled"], 0)
        self.assertGreaterEqual(body["skipped"], 1)


class TestFitImportHooksXSS(_BackfillBase):
    """v1.3.6 — _parse_fit_stats invokes compute_ride_xss."""

    def test_fit_parse_calls_compute_ride_xss(self):
        # Patch _parse_fit_stats's runtime so we can verify the hook.
        # We cannot easily synthesize a real FIT file in-test; instead
        # we patch fit_tool.fit_file.FitFile.from_file to return a fake
        # FIT object with the records the parser walks.
        import app as app_module

        # Spy on compute_ride_xss.
        import ride_storage as _rs
        seen: list = []
        orig = _rs.compute_ride_xss

        def spy(power_series, started_at=None, summary=None,
                cp=None, wprime_j=None, pmax=None):
            seen.append({
                "power_len": len(power_series or []),
                "started_at": started_at,
            })
            return orig(power_series, started_at=started_at, summary=summary,
                        cp=cp, wprime_j=wprime_j, pmax=pmax)

        with patch.object(_rs, "compute_ride_xss", side_effect=spy):
            # Build a fake FIT object the parser walks. _parse_fit_stats
            # branches on `type(msg).__name__` so the fake message MUST be
            # an instance of distinct classes named RecordMessage and
            # SessionMessage (not a generic class with __name__ attr).
            class RecordMessage:
                def __init__(self, **kw):
                    self._d = kw
                def get_value(self, k):
                    return self._d.get(k)

            class SessionMessage:
                def __init__(self, **kw):
                    self._d = kw
                def get_value(self, k):
                    return self._d.get(k)

            class _FakeFit:
                @property
                def records(self):
                    msgs = []
                    for i in range(60):
                        rec = type("R", (), {"message": RecordMessage(
                            power=200, heart_rate=145, cadence=85,
                            speed=8.0, distance=i * 8.0,
                        )})()
                        msgs.append(rec)
                    sm = SessionMessage(
                        total_timer_time=60.0, total_distance=480.0,
                        total_work=12000.0, training_stress_score=10.0,
                        sport="cycling",
                        start_time="2026-05-05 08:00:00",
                    )
                    rec = type("R", (), {"message": sm})()
                    msgs.append(rec)
                    return msgs

            with patch("fit_tool.fit_file.FitFile.from_file", return_value=_FakeFit()):
                app_module._parse_fit_stats(Path("/tmp/fake.fit"))

            self.assertEqual(len(seen), 1, "compute_ride_xss called exactly once")
            self.assertEqual(seen[0]["power_len"], 60,
                             "power_series passed in full")


if __name__ == "__main__":
    unittest.main()
