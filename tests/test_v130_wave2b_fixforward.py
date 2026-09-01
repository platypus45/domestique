"""v1.3.0 Wave 2B fix-forward — regression tests for the 4 BLOCKERs + 7 HIGHs
surfaced by /tmp/grill_v130_impl_wave2b.md.

Coverage by grill ID:
  W2B-G1  — duplicate `loadPowerCurve` removed (dashboard.html)
  W2B-G2  — FR insufficient_data carries a `reason` (covered in
             test_fatigue_resistance.py::Test10ReasonField)
  W2B-G3  — PR-badge XSS via attribute interpolation closed (data-attr
             + delegated handler instead of `escJs(prevId)` in onclick)
  W2B-G4  — toast-queue endpoint exists (`GET /api/profile/pr-toast-queue`)
  W2B-G5  — FR endpoint serialises concurrent same-key compute (lock)
  W2B-G6  — kj_threshold ∉ {1500, 2000} → 422 (was silent coerce to 1500)
  W2B-G7  — loadFatigueResistance passes window_days
  W2B-G8  — real bonk test (covered in test_fatigue_resistance.py::Test09RealBonk)
  W2B-G9  — persist hook preserves prior `prs` on compute failure
  W2B-G10 — real `threading.Thread` test of backfill+compute race
  W2B-G11 — PR sort puts `tier='first'` first
  W2B-G15 — `n_long_rides_at_threshold` removed from dashboard reads

Per CLAUDE.md §4 (goal-driven execution) — each test states a verifiable
success criterion in the docstring.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import power_curve
import ride_storage as _rs
import app as app_module


# ───────────────────────────────────────────────────────────────────────────────
# W2B-G1 — duplicate loadPowerCurve declaration removed
# ───────────────────────────────────────────────────────────────────────────────

class W2BG1DuplicateLoadPowerCurve(unittest.TestCase):
    """W2B-G1: dashboard.html had two `async function loadPowerCurve(...)`
    declarations. Hoisting → second wins → fetched a dead route. The fix
    deletes the legacy block. Test asserts at most one declaration exists.
    """

    def test_only_one_loadpowercurve_declaration(self):
        repo = Path(__file__).resolve().parent.parent
        html = (repo / "src" / "templates" / "dashboard.html").read_text(
            encoding="utf-8")
        decls = re.findall(r"^async function loadPowerCurve\(",
                           html, re.MULTILINE)
        self.assertEqual(len(decls), 1,
                          f"expected 1 loadPowerCurve declaration, got "
                          f"{len(decls)}")
        # The legacy implementation hit /api/power-curve?days=...&compare_days=
        # via fetch(...). That fetch call must be gone — only the leftover
        # comment in the W2B-G1 fix marker is allowed to mention the URL.
        self.assertNotIn("fetch('/api/power-curve?days=", html)
        self.assertNotIn('fetch("/api/power-curve?days=', html)


# ───────────────────────────────────────────────────────────────────────────────
# W2B-G3 — PR badge XSS hardening
# ───────────────────────────────────────────────────────────────────────────────

class W2BG3PRBadgeXSS(unittest.TestCase):
    """W2B-G3: `escJs(prevId)` was interpolated INSIDE an HTML attribute
    (`onclick="openRideDetail('...')"`) — escJs handles JS strings but not
    HTML-attribute breakouts. Fix: data-attribute + delegated click.
    """

    def test_pr_badge_uses_data_attr_not_inline_onclick(self):
        repo = Path(__file__).resolve().parent.parent
        html = (repo / "src" / "templates" / "dashboard.html").read_text(
            encoding="utf-8")
        # The vulnerable pattern was:
        #   `onclick="openRideDetail('${escJs(prevId)}')"`
        self.assertNotIn("openRideDetail('${escJs(prevId)}')", html,
                         "inline onclick with escJs(prevId) is XSS-shaped")
        # The replacement uses data-prev-ride-id + a delegated handler.
        self.assertIn('data-prev-ride-id="${esc(prevId)}"', html)
        # The delegated registration uses dataset.prDelegated as a guard.
        self.assertIn("dataset.prDelegated", html,
                       "expected delegated click registration")
        # And reads the ride id back via getAttribute('data-prev-ride-id').
        self.assertIn("getAttribute('data-prev-ride-id')", html)


# ───────────────────────────────────────────────────────────────────────────────
# W2B-G4 — toast-queue endpoint exists
# ───────────────────────────────────────────────────────────────────────────────

class W2BG4ToastQueueEndpoint(unittest.TestCase):
    """W2B-G4: `_maybe_queue_pr_toasts` writes the queue but no endpoint
    drains it. Fix adds `GET /api/profile/pr-toast-queue?drain=1`.
    """

    def setUp(self):
        self.client = TestClient(app_module.app)

    def test_endpoint_returns_queue_shape(self):
        # The queue file may or may not exist — either way the endpoint
        # returns a well-shaped JSON.
        r = self.client.get("/api/profile/pr-toast-queue")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("queue", body)
        self.assertIn("count", body)
        self.assertIsInstance(body["queue"], list)
        self.assertIsInstance(body["count"], int)

    def test_dashboard_wires_drain_function(self):
        repo = Path(__file__).resolve().parent.parent
        html = (repo / "src" / "templates" / "dashboard.html").read_text(
            encoding="utf-8")
        # The toast-queue path must appear in a fetch call.
        self.assertIn("/api/profile/pr-toast-queue?drain=1", html)
        # And the drain function must be defined.
        self.assertIn("drainPRToastQueue", html)


# ───────────────────────────────────────────────────────────────────────────────
# W2B-G5 — FR endpoint serialises concurrent same-key compute
# ───────────────────────────────────────────────────────────────────────────────

class W2BG5ConcurrentSameKey(unittest.TestCase):
    """W2B-G5: two concurrent dashboard polls for the same (window,
    threshold) pair both computed. Fix adds a per-key lock so only the
    first computes; subsequent reads hit the cache.
    """

    def setUp(self):
        self.client = TestClient(app_module.app)

    def test_concurrent_requests_serialised(self):
        # Reset cache so the first request actually computes.
        for k in list(app_module._cache.keys()):
            if k.startswith("fatigue_resistance_"):
                app_module._cache.pop(k, None)
                app_module._cache_ts.pop(k, None)
        # v1.8.9 Bug 4 — also clear the lru_cache wrapper added in v1.8.9
        # so the patched compute is actually invoked.
        try:
            app_module._fatigue_resistance_memoised.cache_clear()
        except Exception:
            pass

        compute_count = {"n": 0}
        real_compute = power_curve.compute_fatigue_resistance

        def counting_compute(*args, **kwargs):
            compute_count["n"] += 1
            # Add a tiny delay to widen the race window.
            time.sleep(0.05)
            return real_compute(*args, **kwargs)

        with patch.object(power_curve, "compute_fatigue_resistance",
                           side_effect=counting_compute):
            results = []

            def hit():
                r = self.client.get(
                    "/api/profile/fatigue-resistance?kj_threshold=1500")
                results.append(r.status_code)

            threads = [threading.Thread(target=hit) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

        # All 4 must succeed.
        self.assertEqual(results, [200, 200, 200, 200])
        # Only ONE compute_fatigue_resistance call — others hit cache.
        self.assertEqual(compute_count["n"], 1,
                          f"expected 1 compute, got {compute_count['n']}")


# ───────────────────────────────────────────────────────────────────────────────
# W2B-G6 — kj_threshold ∉ {1500, 2000} → 422
# ───────────────────────────────────────────────────────────────────────────────

class W2BG6KjThresholdValidation(unittest.TestCase):
    """W2B-G6: previously the endpoint silently coerced 800 → 1500. The
    response said `kj_threshold:1500` while the request had asked for 800.
    Fix: 422 with explicit `valid` list.
    """

    def setUp(self):
        self.client = TestClient(app_module.app)

    def test_kj_threshold_800_rejected(self):
        r = self.client.get(
            "/api/profile/fatigue-resistance?kj_threshold=800")
        self.assertEqual(r.status_code, 422)
        body = r.json()
        # FastAPI wraps HTTPException detail in {"detail": ...}.
        detail = body.get("detail") or body
        self.assertEqual(detail.get("error"), "invalid_kj_threshold")
        self.assertEqual(detail.get("received"), 800)
        self.assertEqual(detail.get("valid"), [1500, 2000])

    def test_valid_thresholds_accepted(self):
        for kj in (1500, 2000):
            r = self.client.get(
                f"/api/profile/fatigue-resistance?kj_threshold={kj}")
            self.assertIn(r.status_code, (200,))
            body = r.json()
            self.assertEqual(body["kj_threshold"], kj)


# ───────────────────────────────────────────────────────────────────────────────
# W2B-G7 — loadFatigueResistance passes window_days
# ───────────────────────────────────────────────────────────────────────────────

class W2BG7WindowDaysPropagated(unittest.TestCase):
    """W2B-G7: the dashboard fetcher hardcoded `?kj_threshold=...` and
    forgot `window_days`. The endpoint then defaulted to 365 even though
    the user might want a different window. Fix passes window_days
    explicitly.
    """

    def test_dashboard_passes_window_days(self):
        repo = Path(__file__).resolve().parent.parent
        html = (repo / "src" / "templates" / "dashboard.html").read_text(
            encoding="utf-8")
        # The fetch URL must include window_days now.
        self.assertIn("window_days=", html,
                       "loadFatigueResistance must pass window_days")
        # Specifically inside loadFatigueResistance — find the function
        # body and assert window_days appears within it.
        m = re.search(
            r"async function loadFatigueResistance.*?\n\}",
            html, re.DOTALL)
        self.assertIsNotNone(m,
                              "loadFatigueResistance not found in dashboard")
        body = m.group(0)
        self.assertIn("window_days=", body)
        self.assertIn("_fatigueResistanceWindowDays", body)


# ───────────────────────────────────────────────────────────────────────────────
# W2B-G9 — persist hook preserves prior `prs` on compute failure
# ───────────────────────────────────────────────────────────────────────────────

class W2BG9PersistHookPreservesPriorPRs(unittest.TestCase):
    """W2B-G9: when `compute_ride_prs` raised, persist_icu_activity
    overwrote the on-disk envelope with `norm` which had no `prs[]` →
    silent loss of prior PRs. Fix reads prior `prs` first and merges
    forward on raise.
    """

    def setUp(self):
        self._tmp = Path(os.environ.get("TMPDIR", "/tmp")) / \
            f"rs_{os.getpid()}_{id(self)}"
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._patch_dir = patch.object(_rs, "_icu_rides_dir",
                                        return_value=self._tmp)
        self._patch_dir.start()

    def tearDown(self):
        self._patch_dir.stop()
        for f in self._tmp.glob("*"):
            f.unlink(missing_ok=True)
        self._tmp.rmdir()

    def test_compute_failure_preserves_prior_prs(self):
        # Pre-seed an envelope with prs[].
        prior_prs = [{"duration_s": 300, "today_w": 280, "tier": "major"}]
        envelope = {
            "id": "i9999",
            "ride_id": "icu_i9999",
            "external_id": "i9999",
            "type": "Ride",
            "name": "Fix-forward W2B-G9",
            "startTime": "2026-05-05T10:00:00.000Z",
            "duration": 3600,
            "movingTime": 3500,
            "distance": 50000,
            "icu_average_watts": 200,
            "icu_normalized_power": 220,
            "icu_training_load": 80,
            "icu_joules": 720000,
            "average_heartrate": 140,
            "max_heartrate": 165,
            "icu_zone_times": [{"id": "Z2", "secs": 1800}],
            "icu_intervals": [],
            "prs": prior_prs,
        }
        (self._tmp / "i9999.json").write_text(
            json.dumps(envelope), encoding="utf-8")

        # Force compute_ride_prs to raise.
        with patch.object(power_curve, "compute_ride_prs",
                           side_effect=RuntimeError("boom")):
            path = _rs.persist_icu_activity(envelope)
        self.assertIsNotNone(path)
        # Re-read — prior prs[] preserved.
        rec = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(rec.get("prs"), prior_prs,
                          "prior prs lost on compute_ride_prs raise")


# ───────────────────────────────────────────────────────────────────────────────
# W2B-G10 — REAL threading.Thread test
# ───────────────────────────────────────────────────────────────────────────────

class W2BG10RealThreadingBackfillRace(unittest.TestCase):
    """W2B-G10: Wave 2A grill (G8) noted "concurrent" tests were
    single-threaded. This test forks two real threads — one writing a
    ride file, one reading it — to verify the reader doesn't crash on
    JSONDecodeError mid-write.
    """

    def setUp(self):
        self._tmp = Path(os.environ.get("TMPDIR", "/tmp")) / \
            f"thread_{os.getpid()}_{id(self)}"
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._patch_dir = patch.object(power_curve, "_icu_rides_dir",
                                        return_value=self._tmp)
        self._patch_dir.start()
        self._patch_prof = patch.object(power_curve, "_profile_ftp_weight",
                                         return_value=(250, 70.0))
        self._patch_prof.start()

    def tearDown(self):
        self._patch_dir.stop()
        self._patch_prof.stop()
        for f in self._tmp.glob("*"):
            f.unlink(missing_ok=True)
        self._tmp.rmdir()

    def test_reader_survives_concurrent_writes(self):
        # Pre-seed 4 healthy rides so compute is meaningful.
        today = date.today()
        for i in range(4):
            day = today - timedelta(days=10 + i * 3)
            ride = {
                "ride_id": f"icu_iX{i + 100}",
                "external_id": f"iX{i + 100}",
                "source": "icu",
                "started_at": f"{day.isoformat()}T08:00:00",
                "kj": 1700.0,
                "ftp_at_ride": 250,
                "weight_kg": 70.0,
                "efforts": [{"secs": 300, "watts": 270, "hr_avg": 165}],
            }
            (self._tmp / f"{ride['external_id']}.json").write_text(
                json.dumps(ride), encoding="utf-8")

        stop_writer = threading.Event()

        def writer():
            i = 0
            while not stop_writer.is_set():
                # Simulate atomic-write: write to .tmp, rename. We
                # deliberately interleave many small writes to widen the
                # race window.
                fname = f"iY{i}.json"
                tmp = self._tmp / f"{fname}.tmp"
                final = self._tmp / fname
                ride = {
                    "ride_id": f"icu_iY{i}",
                    "external_id": f"iY{i}",
                    "source": "icu",
                    "started_at": f"{today.isoformat()}T08:00:00",
                    "kj": 800.0,
                    "ftp_at_ride": 250,
                    "weight_kg": 70.0,
                }
                tmp.write_text(json.dumps(ride), encoding="utf-8")
                tmp.rename(final)
                i += 1
                if i > 50:
                    break

        errors = []

        def reader():
            for _ in range(20):
                try:
                    power_curve.aggregate_power_curve(
                        "default", window_days=90)
                except Exception as e:
                    errors.append(str(e))
                time.sleep(0.005)

        wt = threading.Thread(target=writer)
        rt = threading.Thread(target=reader)
        wt.start()
        rt.start()
        rt.join(timeout=15.0)
        stop_writer.set()
        wt.join(timeout=15.0)

        self.assertEqual(errors, [],
                          f"reader crashed during concurrent writes: "
                          f"{errors}")


# ───────────────────────────────────────────────────────────────────────────────
# W2B-G11 — PR sort puts tier='first' first
# ───────────────────────────────────────────────────────────────────────────────

class W2BG11FirstTierSortOrder(unittest.TestCase):
    """W2B-G11: previous sort `Number(b.exceedance_pct||0)` left
    `tier='first'` (null exceedance → 0) at the bottom. Fix promotes
    first-tier above major/minor.
    """

    def test_sort_promotes_first_tier(self):
        repo = Path(__file__).resolve().parent.parent
        html = (repo / "src" / "templates" / "dashboard.html").read_text(
            encoding="utf-8")
        # The new sort uses _tierRank.
        self.assertIn("_tierRank", html)
        # The sort comparator must rank first > major > minor.
        self.assertIn("=== 'first' ? 2 : (t === 'major' ? 1 : 0)", html)


# ───────────────────────────────────────────────────────────────────────────────
# W2B-G15 — n_long_rides_at_threshold removed
# ───────────────────────────────────────────────────────────────────────────────

class W2BG15RemovedDeadField(unittest.TestCase):
    """W2B-G15: the renderer read `d.n_long_rides_at_threshold` but the
    API never set it — silent dead-branch. Fix removes the read.
    """

    def test_dashboard_no_longer_reads_n_long_rides_at_threshold(self):
        repo = Path(__file__).resolve().parent.parent
        html = (repo / "src" / "templates" / "dashboard.html").read_text(
            encoding="utf-8")
        # The dead read pattern was `d.n_long_rides_at_threshold`.
        # The fix-forward comment may mention the field name so we only
        # ban the actual property access form.
        self.assertNotIn("d.n_long_rides_at_threshold", html)
        self.assertNotIn("n_long_rides_at_threshold ||", html)


if __name__ == "__main__":
    unittest.main()
