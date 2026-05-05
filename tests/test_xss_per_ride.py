"""v1.0.6 IMPL-3D-INGEST — per-ride XSS decomposition tests.

Verifies the post-ride hook ``ride_storage.compute_ride_xss()``:

  1. Importing a synthetic ride with power trace writes ``xss_total`` /
     ``xss_cp`` / ``xss_w_prime`` / ``xss_pmax`` into the cached ride
     summary dict.
  2. Per-day aggregates land in ``athlete_metrics`` with the right
     metric names (``ss_cp_daily`` / ``ss_w_prime_daily`` /
     ``ss_pmax_daily``).
  3. A ride without a power trace doesn't crash — the function returns
     ``{}`` and the summary dict carries None placeholders so the UI can
     render "no power data" rather than a misleading 0.

``strain_score.compute_xss_components`` is NEW (owned by IMPL-3D-MODEL).
At the time these tests are first run that module may not yet exist on
disk, so we install a fake ``strain_score`` module via ``sys.modules``
to keep the integration test forward-compatible. The Wave 3 QA agent
will land a real-call integration test against the actual model.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _make_base(tmp: str) -> Path:
    base = Path(tmp) / ".domestique"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _bootstrap_profile(base: Path) -> None:
    p = base / "profiles" / "default"
    p.mkdir(parents=True, exist_ok=True)
    (p / "athlete.json").write_text(json.dumps({
        "ftp": 250, "weight_kg": 72.0, "lbm_kg": 58.0,
        "lthr": 175, "max_hr": 196,
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
    """Install a stand-in for strain_score.compute_xss_components.

    Writes back the partition we'd expect from §1 formulas: SS_CP +
    SS_W' + SS_Pmax sums to SS_total. Numeric values are illustrative,
    not validated — the goal is to exercise the wiring, not the model.
    """
    fake = types.ModuleType("strain_score")

    def compute_xss_components(power_series, cp, wprime_j, pmax):
        # Crude partition: anything <= CP is aerobic; anything above CP
        # is split 70/30 between glycolytic and PCr by power magnitude.
        # Per-second sum scaled to TSS-equivalent so the numbers look
        # realistic (≈100/h at CP).
        if not power_series:
            return {}
        total = 0.0
        cp_part = 0.0
        wp_part = 0.0
        pm_part = 0.0
        for p in power_series:
            try:
                pv = float(p)
            except (TypeError, ValueError):
                pv = 0.0
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

    fake.compute_xss_components = compute_xss_components
    sys.modules["strain_score"] = fake


def _uninstall_fake_strain_score():
    sys.modules.pop("strain_score", None)


class _ScaffoldedTest(unittest.TestCase):
    """Tmp DB + tmp profile + fake strain_score module."""

    def setUp(self):
        import db
        from profile_manager import ProfileManager
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_profile(_make_base(self.tmp))
        self._home_patch = patch("pathlib.Path.home", return_value=Path(self.tmp))
        self._home_patch.start()
        ProfileManager._instance = None
        self.pm = ProfileManager.get()
        self.dbfile = Path(self.tmp) / "t.db"
        db.set_db_path(self.dbfile)
        db.close_all_connections()
        db.init_db()
        _install_fake_strain_score()

    def tearDown(self):
        from profile_manager import ProfileManager
        _uninstall_fake_strain_score()
        self._home_patch.stop()
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSyntheticRideWithPowerTrace(_ScaffoldedTest):
    """§1 — compute_ride_xss writes xss_* fields into the summary dict."""

    def test_synthetic_ride_writes_xss_into_summary(self):
        import ride_storage
        # 1-hour ride: alternating Z2 / over-FTP intervals (cap at 1Hz so
        # the fake strain model has something to chew on).
        power_series = [180] * 1800 + [320] * 600 + [200] * 1200
        summary: dict = {"tss": 95}
        components = ride_storage.compute_ride_xss(
            power_series=power_series,
            started_at="2026-05-01T08:00:00",
            summary=summary,
            cp=250, wprime_j=20000, pmax=1100,
        )
        self.assertIn("xss_total", components)
        self.assertIn("xss_cp", components)
        self.assertIn("xss_w_prime", components)
        self.assertIn("xss_pmax", components)
        self.assertGreater(components["xss_total"], 0)
        # Summary cached in place
        self.assertEqual(summary["xss_total"], components["xss_total"])
        self.assertEqual(summary["xss_cp"], components["xss_cp"])
        self.assertEqual(summary["xss_w_prime"], components["xss_w_prime"])
        self.assertEqual(summary["xss_pmax"], components["xss_pmax"])


class TestPerDayAggregatesWritten(_ScaffoldedTest):
    """§2 — per-day aggregates land in athlete_metrics under the spec'd names."""

    def test_per_day_aggregates_in_athlete_metrics(self):
        import ride_storage
        import db
        power_series = [180] * 1800 + [320] * 600
        ride_storage.compute_ride_xss(
            power_series=power_series,
            started_at="2026-05-01T08:00:00",
            summary={},
            cp=250, wprime_j=20000, pmax=1100,
        )
        conn = db.get_db()
        rows = {
            r["metric"]: (r["value"], r["source"])
            for r in conn.execute(
                "SELECT metric, value, source FROM athlete_metrics WHERE date = '2026-05-01'"
            ).fetchall()
        }
        # Three rows expected: one per energy system.
        self.assertIn("ss_cp_daily", rows)
        self.assertIn("ss_w_prime_daily", rows)
        self.assertIn("ss_pmax_daily", rows)
        for m, (val, src) in rows.items():
            self.assertEqual(src, "computed", f"{m} source")
            self.assertGreater(val, 0)


class TestRideWithoutPowerTraceGraceful(_ScaffoldedTest):
    """§3 — empty / missing power trace → graceful no-op."""

    def test_empty_power_series_returns_empty_dict(self):
        import ride_storage
        summary: dict = {"tss": 0}
        out = ride_storage.compute_ride_xss(
            power_series=[],
            started_at="2026-05-02T08:00:00",
            summary=summary,
            cp=250, wprime_j=20000, pmax=1100,
        )
        self.assertEqual(out, {})
        # Summary gets None placeholders so the UI can render "no power data"
        # instead of mistakenly treating it as zero.
        self.assertIsNone(summary["xss_total"])
        self.assertIsNone(summary["xss_cp"])
        self.assertIsNone(summary["xss_w_prime"])
        self.assertIsNone(summary["xss_pmax"])

    def test_all_zero_power_series_no_crash(self):
        import ride_storage
        out = ride_storage.compute_ride_xss(
            power_series=[0, 0, 0, 0, 0],
            started_at="2026-05-03T08:00:00",
            summary={},
            cp=250, wprime_j=20000, pmax=1100,
        )
        self.assertEqual(out, {})

    def test_no_strain_score_module_no_crash(self):
        """If strain_score isn't on the path yet (cross-agent ordering), the
        helper should log + return {} rather than raising ImportError."""
        import ride_storage
        # Remove the fake.
        _uninstall_fake_strain_score()
        try:
            out = ride_storage.compute_ride_xss(
                power_series=[200] * 60,
                started_at="2026-05-04T08:00:00",
                summary={},
                cp=250, wprime_j=20000, pmax=1100,
            )
            self.assertEqual(out, {})
        finally:
            _install_fake_strain_score()


class TestPmaxWUsedAsDefault(_ScaffoldedTest):
    """When cp/wprime/pmax aren't supplied, the helper reads from the active
    profile — making sure the new pmax_w property is wired in."""

    def test_defaults_pulled_from_profile(self):
        import ride_storage
        # Set an explicit pmax via the profile so we can detect the read.
        self.pm._set_pmax(1115, "icu")
        captured = {}

        import strain_score as _ss

        def _capture(power_series, cp, wprime_j, pmax):
            captured["cp"] = cp
            captured["wprime_j"] = wprime_j
            captured["pmax"] = pmax
            return {
                "xss_total": 1.0, "xss_cp": 1.0,
                "xss_w_prime": 0.0, "xss_pmax": 0.0,
            }

        with patch.object(_ss, "compute_xss_components", side_effect=_capture):
            ride_storage.compute_ride_xss(
                power_series=[200] * 60,
                started_at="2026-05-05T08:00:00",
                summary={},
            )
        self.assertEqual(captured["pmax"], 1115)
        self.assertEqual(captured["cp"], self.pm.cp)
        self.assertEqual(captured["wprime_j"], self.pm.wprime_j)


if __name__ == "__main__":
    unittest.main()
