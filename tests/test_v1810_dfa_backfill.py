"""v1.8.10 — /api/profile/dfa-backfill endpoint + augment lazy-chain tests.

Covers:

1.  ``compute_dfa_alpha1_from_hrv_stream`` returns dict with
    ``rr_intervals_count == 0`` and ``dfa_alpha1_status == 'no_rr_data'``
    when the stream is empty.

2.  ``_augment_icu_record_with_dfa`` skips when status is sticky
    (computed / no_rr_data / sanity_rejected / icu_deleted) unless
    ``force=True`` is passed.

3.  ``_augment_icu_record_with_dfa`` marks ``icu_deleted`` when ALL three
    augment paths (local stream, fresh stream, FIT) fail.

4.  ``/api/profile/dfa-backfill`` returns ``{"status": "started",
    "task_id": ...}`` on first call, ``{"status": "already_running"}``
    on second concurrent call.

5.  ``/api/profile/dfa-backfill/status`` reflects ``state: done`` with
    counters after worker completes.

6.  ``/api/profile/dfa-backfill/cancel`` returns
    ``{"status": "cancel_requested"}`` for a known task_id.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as _app_mod


def _seed_icu_ride(dir_: Path, ext_id: str, status: "str | None" = None,
                    started_at: str = "2026-05-21T10:00:00",
                    algo_version: "int | None" = None) -> Path:
    p = dir_ / f"{ext_id}.json"
    rec = {
        "external_id": ext_id,
        "started_at": started_at,
        "duration_s": 3600,
    }
    if status is not None:
        rec["dfa_alpha1_status"] = status
        rec.setdefault("dfa_alpha1_avg", None)
    # v1.8.14 — a record is sticky only when stamped with the CURRENT algo
    # version. Default to the current version so "sticky status" tests behave
    # as intended; pass an older version to exercise the auto-recompute path.
    if algo_version is not None:
        rec["dfa_algo_version"] = algo_version
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


class TestAugmentIcuDeletedSticky(unittest.TestCase):
    """Augment marks ``icu_deleted`` when streams + FIT both fail."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="dfa_bf_"))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_all_paths_fail_marks_icu_deleted(self):
        p = _seed_icu_ride(self._tmp, "iDELETED", status="fetch_failed")
        with patch("training.fetch_activity_streams", return_value=None), \
             patch("training.fetch_activity_fit_file", return_value=None):
            _app_mod._augment_icu_record_with_dfa(p, "iDELETED")
        rec = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(rec["dfa_alpha1_status"], "icu_deleted")
        self.assertIsNone(rec["dfa_alpha1_avg"])

    def test_sticky_status_skips_without_force(self):
        # Stamped with the CURRENT algo version → genuinely sticky.
        p = _seed_icu_ride(self._tmp, "iSTICKY", status="computed",
                            algo_version=_app_mod._DFA_ALGO_VERSION)
        # Streams fetcher would explode if called → asserts we don't call it.
        with patch("training.fetch_activity_streams",
                    side_effect=AssertionError("should not be called")), \
             patch("training.fetch_activity_fit_file",
                    side_effect=AssertionError("should not be called")):
            _app_mod._augment_icu_record_with_dfa(p, "iSTICKY")  # no force
        rec = json.loads(p.read_text(encoding="utf-8"))
        # Untouched.
        self.assertEqual(rec["dfa_alpha1_status"], "computed")

    def test_stale_algo_version_recomputes_without_force(self):
        # v1.8.14 — a "computed" record stamped with an OLD algo version is
        # NOT sticky: augment must re-run so the artifact-filter fix heals it.
        # Patched fetchers return nothing → it lands on icu_deleted, proving
        # the augment path executed despite status='computed'.
        p = _seed_icu_ride(self._tmp, "iSTALE", status="computed",
                            algo_version=_app_mod._DFA_ALGO_VERSION - 1)
        with patch("training.fetch_activity_streams", return_value=None), \
             patch("training.fetch_activity_fit_file", return_value=None):
            _app_mod._augment_icu_record_with_dfa(p, "iSTALE")  # no force
        rec = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(rec["dfa_alpha1_status"], "icu_deleted")
        self.assertEqual(rec["dfa_algo_version"], _app_mod._DFA_ALGO_VERSION)

    def test_force_retries_even_sticky(self):
        p = _seed_icu_ride(self._tmp, "iFORCE", status="icu_deleted")
        with patch("training.fetch_activity_streams", return_value=None), \
             patch("training.fetch_activity_fit_file", return_value=None):
            _app_mod._augment_icu_record_with_dfa(p, "iFORCE", force=True)
        rec = json.loads(p.read_text(encoding="utf-8"))
        # Still icu_deleted because the patched fetches return None — but
        # the augment ATTEMPTED, proving force=True bypasses sticky gate.
        self.assertEqual(rec["dfa_alpha1_status"], "icu_deleted")

    def test_lazy_stream_path_succeeds_without_fit(self):
        """When cached streams.hrv is present, augment should NEVER hit
        the FIT path — proves the lazy compute chain is wired."""
        p = _seed_icu_ride(self._tmp, "iLAZY", status="fetch_failed")
        rec = json.loads(p.read_text(encoding="utf-8"))
        # Seed a tiny but valid hrv stream that yields >0 RR.
        rec["streams"] = {"hrv": [[800], [820], [810], [805]] * 100}
        p.write_text(json.dumps(rec), encoding="utf-8")

        fit_called = []
        def _fit_die(_id):
            fit_called.append(_id)
            return None
        with patch("training.fetch_activity_streams",
                    side_effect=AssertionError("should not be called")), \
             patch("training.fetch_activity_fit_file", side_effect=_fit_die):
            _app_mod._augment_icu_record_with_dfa(p, "iLAZY")

        rec2 = json.loads(p.read_text(encoding="utf-8"))
        # Stream is too short for valid DFA but the path WAS taken — status
        # transitions out of 'fetch_failed' to something stream-derived.
        self.assertIn(rec2["dfa_alpha1_status"],
                       ("no_rr_data", "computed", "sanity_rejected"))
        self.assertEqual(fit_called, [], "FIT fallback fired despite cached stream")


class TestDfaBackfillEndpoint(unittest.TestCase):
    """POST/GET /api/profile/dfa-backfill — start, poll, cancel."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="dfa_bf_ep_"))
        # Redirect _user_data_dir → tmp so rides/icu/ resolves into our fixture.
        self._patch_data_dir = patch.object(_app_mod, "_user_data_dir",
                                              self._tmp)
        self._patch_data_dir.start()
        (self._tmp / "rides" / "icu").mkdir(parents=True, exist_ok=True)
        # Clear in-memory task table between tests.
        with _app_mod._dfa_backfill_thread_lock:
            _app_mod._dfa_backfill_tasks.clear()
        _app_mod._dfa_backfill_cancel.clear()
        # Reset single-flight lock just in case a previous test left it held.
        try:
            _app_mod._dfa_backfill_lock.release()
        except RuntimeError:
            pass
        self._client = TestClient(_app_mod.app)

    def tearDown(self):
        self._patch_data_dir.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)
        try:
            _app_mod._dfa_backfill_lock.release()
        except RuntimeError:
            pass

    def test_empty_dir_done_immediately(self):
        """No ICU rides → worker exits immediately with state=done."""
        r = self._client.post("/api/profile/dfa-backfill")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "started")
        task_id = body["task_id"]

        # Poll until done (cap 3 s).
        for _ in range(30):
            rs = self._client.get(
                f"/api/profile/dfa-backfill/status?task_id={task_id}"
            )
            self.assertEqual(rs.status_code, 200)
            s = rs.json()
            if s.get("state") == "done":
                break
            time.sleep(0.1)
        self.assertEqual(s.get("state"), "done")
        self.assertEqual(s.get("total"), 0)
        self.assertEqual(s.get("candidates"), 0)

    def test_single_flight_returns_already_running(self):
        """Second concurrent POST must return already_running.

        Block the worker on a threading.Event so we have a stable window
        in which the lock is held — avoids flaky timing race where the
        worker finishes before the second POST lands.
        """
        import threading as _threading
        _seed_icu_ride(self._tmp / "rides" / "icu", "iA", status=None)

        gate = _threading.Event()
        def _hold_augment(rec_path, ext_id, force=False):
            # Block until the test releases — simulates a slow ICU fetch.
            gate.wait(timeout=2.0)

        with patch.object(_app_mod, "_augment_icu_record_with_dfa",
                            side_effect=_hold_augment):
            r1 = self._client.post("/api/profile/dfa-backfill")
            self.assertEqual(r1.json()["status"], "started")
            r2 = self._client.post("/api/profile/dfa-backfill")
            self.assertEqual(r2.json()["status"], "already_running",
                "second POST while worker holds the lock must report already_running")
            # Release the worker.
            gate.set()
            # Drain to done so other tests start clean.
            tid = r1.json()["task_id"]
            for _ in range(50):
                rs = self._client.get(
                    f"/api/profile/dfa-backfill/status?task_id={tid}"
                )
                if rs.json().get("state") in ("done", "cancelled", "error"):
                    break
                time.sleep(0.05)

    def test_cancel_endpoint_returns_cancel_requested(self):
        r = self._client.post("/api/profile/dfa-backfill")
        tid = r.json()["task_id"]
        rc = self._client.post(
            f"/api/profile/dfa-backfill/cancel?task_id={tid}"
        )
        self.assertEqual(rc.status_code, 200)
        self.assertEqual(rc.json()["status"], "cancel_requested")

    def test_cancel_unknown_task_returns_404(self):
        rc = self._client.post(
            "/api/profile/dfa-backfill/cancel?task_id=ZZZ_NONE"
        )
        self.assertEqual(rc.status_code, 404)

    def test_status_unknown_task_returns_404(self):
        rs = self._client.get(
            "/api/profile/dfa-backfill/status?task_id=ZZZ_NONE"
        )
        self.assertEqual(rs.status_code, 404)


def _seed_dfa_ride(dir_: Path, ext_id: str, *, date="2026-05-20T10:00:00",
                   name="Ride", alpha=1.0, status="computed", version=3,
                   hrvt1=None, hrvt2=None, zones=None, avg_hr=140, moving_s=3600):
    """Seed an ICU envelope shaped like a real v3 DFA record."""
    rec = {
        "external_id": ext_id, "id": ext_id, "started_at": date, "name": name,
        "avg_hr": avg_hr, "moving_s": moving_s, "duration_s": moving_s + 120,
        "dfa_alpha1_avg": alpha, "dfa_alpha1_status": status,
        "dfa_alpha1_lt1_minutes": 0.0, "rr_intervals_count": 5000,
        "dfa_algo_version": version,
        "dfa_hrvt1": hrvt1, "dfa_hrvt2": hrvt2,
        "dfa_zone_minutes": zones or {"z1": 50.0, "z2": 5.0, "z3": 0.0},
    }
    (dir_ / f"{ext_id}.json").write_text(json.dumps(rec), encoding="utf-8")


class TestDfaRidesEndpoint(unittest.TestCase):
    """v1.8.14 — /api/profile/dfa-rides shape + per-channel aggregate."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="dfa_rides_"))
        self._patch = patch.object(_app_mod, "_user_data_dir", self._tmp)
        self._patch.start()
        self._icu = self._tmp / "rides" / "icu"
        self._icu.mkdir(parents=True, exist_ok=True)
        self._client = TestClient(_app_mod.app)

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_empty_archive(self):
        r = self._client.get("/api/profile/dfa-rides").json()
        self.assertEqual(r["rides"], [])
        self.assertIsNone(r["aggregate"]["hrvt1"])
        self.assertIsNone(r["aggregate"]["hrvt2"])
        self.assertEqual(r["n_computed"], 0)

    def test_passthrough_and_per_channel_aggregate(self):
        # 3 rides: two with good-r² power HRVT1, one with low-r² HR only.
        good1 = {"alpha": 0.75, "hr": 165.0, "r2_hr": 0.62,
                 "power": 240.0, "r2_power": 0.60}
        good2 = {"alpha": 0.75, "hr": 168.0, "r2_hr": 0.55,
                 "power": 250.0, "r2_power": 0.58}
        lowr2 = {"alpha": 0.75, "hr": 150.0, "r2_hr": 0.31,  # below 0.50 gate
                 "power": None, "r2_power": None}
        _seed_dfa_ride(self._icu, "iA", date="2026-05-20T10:00:00", hrvt1=good1)
        _seed_dfa_ride(self._icu, "iB", date="2026-05-19T10:00:00", hrvt1=good2)
        _seed_dfa_ride(self._icu, "iC", date="2026-05-18T10:00:00", hrvt1=lowr2)
        r = self._client.get("/api/profile/dfa-rides").json()
        # Pass-through: row hrvt1 equals the stored dict exactly.
        row_a = next(x for x in r["rides"] if x["id"] == "iA")
        self.assertEqual(row_a["hrvt1"], good1)
        # Date is a server-side [:10] slice, no shift.
        self.assertEqual(row_a["date"], "2026-05-20")
        # duration from moving_s/60.
        self.assertEqual(row_a["duration_min"], 60)
        # v2.4.1 — aggregate is now an r²-CONFIDENCE-WEIGHTED median over the two
        # good rides (was a plain median). good1 (240 W / 165 bpm) has higher r²
        # than good2 (250 W / 168 bpm), so the weighted median lands on good1's
        # values, NOT the plain median (245 / 166.5). The low-r² ride (iC) is
        # excluded from the aggregate but present in rides.
        agg = r["aggregate"]["hrvt1"]
        self.assertEqual(agg["power"], 240.0)
        self.assertEqual(agg["n_power"], 2)
        self.assertEqual(agg["hr"], 165.0)
        self.assertEqual(agg["n_hr"], 2)
        self.assertEqual(len(r["rides"]), 3)

    def test_steady_ride_no_threshold_is_not_error(self):
        # Steady ride: α1 high, no HRVT resolved → row present, hrvt1 None,
        # aggregate None. Must NOT error.
        _seed_dfa_ride(self._icu, "iSteady", alpha=1.15, hrvt1=None, hrvt2=None)
        r = self._client.get("/api/profile/dfa-rides").json()
        self.assertEqual(len(r["rides"]), 1)
        self.assertIsNone(r["rides"][0]["hrvt1"])
        self.assertIsNone(r["aggregate"]["hrvt1"])

    def test_stale_version_counted(self):
        _seed_dfa_ride(self._icu, "iOld", version=2,
                       hrvt1={"alpha": 0.75, "hr": 160.0, "r2_hr": 0.6,
                              "power": 230.0, "r2_power": 0.6})
        _seed_dfa_ride(self._icu, "iNew", version=_app_mod._DFA_ALGO_VERSION,
                       hrvt1={"alpha": 0.75, "hr": 162.0, "r2_hr": 0.6,
                              "power": 235.0, "r2_power": 0.6})
        r = self._client.get("/api/profile/dfa-rides").json()
        self.assertEqual(r["n_stale_version"], 1)
        self.assertEqual(r["n_computed"], 2)


if __name__ == "__main__":
    unittest.main()
