"""v2.2.11 — the activity sync must be serialized so overlapping runs don't
clobber the shared progress global (the home banner bounced "11 of 49 → 30 → 16…"
because the forced /api/rides/sync path didn't share the lazy single-flight guard).

A second concurrent `_sync_icu_activities` must return `already_running` WITHOUT
resetting `_icu_sync_progress`.
"""
import unittest

import app


class TestSyncSerialize(unittest.TestCase):
    def test_concurrent_call_returns_already_running(self):
        # Simulate a sync already in flight by holding the exec lock.
        acquired = app._sync_exec_lock.acquire(blocking=False)
        self.assertTrue(acquired, "lock should be free at test start")
        try:
            r = app._sync_icu_activities(force=True)
            self.assertEqual(r.get("status"), "already_running")
            self.assertEqual(r.get("added"), 0)
        finally:
            app._sync_exec_lock.release()

    def test_lock_released_after_normal_call(self):
        # A normal (non-overlapping) call must release the lock so the next sync
        # can run — i.e. the guard isn't sticky. We don't run a real sync (no ICU
        # creds in test env → it returns no_credentials fast), just assert the
        # lock is free before AND after.
        self.assertTrue(app._sync_exec_lock.acquire(blocking=False))
        app._sync_exec_lock.release()
        app._sync_icu_activities(force=False)  # returns quickly (no creds / throttle)
        # Lock must be free again.
        self.assertTrue(app._sync_exec_lock.acquire(blocking=False),
                        "exec lock not released after a sync call")
        app._sync_exec_lock.release()


if __name__ == "__main__":
    unittest.main()
