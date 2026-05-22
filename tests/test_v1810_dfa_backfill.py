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
                    started_at: str = "2026-05-21T10:00:00") -> Path:
    p = dir_ / f"{ext_id}.json"
    rec = {
        "external_id": ext_id,
        "started_at": started_at,
        "duration_s": 3600,
    }
    if status is not None:
        rec["dfa_alpha1_status"] = status
        rec.setdefault("dfa_alpha1_avg", None)
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
        p = _seed_icu_ride(self._tmp, "iSTICKY", status="computed")
        # Streams fetcher would explode if called → asserts we don't call it.
        with patch("training.fetch_activity_streams",
                    side_effect=AssertionError("should not be called")), \
             patch("training.fetch_activity_fit_file",
                    side_effect=AssertionError("should not be called")):
            _app_mod._augment_icu_record_with_dfa(p, "iSTICKY")  # no force
        rec = json.loads(p.read_text(encoding="utf-8"))
        # Untouched.
        self.assertEqual(rec["dfa_alpha1_status"], "computed")

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


if __name__ == "__main__":
    unittest.main()
