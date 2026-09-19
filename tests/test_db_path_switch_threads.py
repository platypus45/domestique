"""Repointing the database takes every thread with it.

Connections live in ``db._local`` (thread-local), so moving ``DB_PATH`` alone
changes what the CALLING thread opens next and nothing else: the request
threads behind TestClient, the sync daemon, any pool thread that already has
a connection keep reading the old file until ``_db_version`` moves.

That cost two CI failures on platypus45/domestique#14. Four fixtures restore
the path in teardown without bumping the version, and the next test then
seeded rows on the main thread while ``/api/readiness`` -- running on a pooled
worker thread still attached to the PREVIOUS test's deleted temp database --
answered "no data" (test_one_fitness_state's two readiness cases). They passed
alone, failed in CI, and looked flaky for as long as the bump was a second
call the caller had to remember.
"""
from __future__ import annotations

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402


def _seed(path: Path, marker: str) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS marker (who TEXT)")
    conn.execute("DELETE FROM marker")
    conn.execute("INSERT INTO marker (who) VALUES (?)", (marker,))
    conn.commit()
    conn.close()


def test_a_worker_thread_follows_the_new_database(tmp_path):
    first, second = tmp_path / "first.db", tmp_path / "second.db"
    _seed(first, "first")
    _seed(second, "second")
    original = db.DB_PATH

    # One worker thread, reused for both reads: the shape of the bug. A pool
    # that handed out a fresh thread the second time would hide it.
    pool = ThreadPoolExecutor(max_workers=1)

    def read_marker() -> str:
        return db.get_db().execute("SELECT who FROM marker").fetchone()[0]

    try:
        db.set_db_path(first)
        assert pool.submit(read_marker).result() == "first"

        db.set_db_path(second)
        assert pool.submit(read_marker).result() == "second", (
            "the worker thread is still reading the previous database: "
            "set_db_path moved DB_PATH without bumping db._db_version, so "
            "every thread that already holds a connection keeps the old one")
    finally:
        pool.shutdown(wait=True)
        db.set_db_path(original)
