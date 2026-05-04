"""Tests for db._maybe_add_column argument validation.

Covers the hardening at db.py:98 that rejects non-identifier table / column
names so a caller passing attacker-controlled strings cannot trigger SQL
injection through the f-string ALTER statement.
"""
from __future__ import annotations

import sqlite3

import pytest

import db


@pytest.fixture()
def conn(tmp_path):
    """In-memory-style DB with one table to ALTER."""
    c = sqlite3.connect(str(tmp_path / "test.db"))
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    yield c
    c.close()


def test_happy_path_adds_column(conn):
    db._maybe_add_column(conn, "t", "new_col", "INTEGER")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(t)").fetchall()]
    assert "new_col" in cols


def test_duplicate_column_is_idempotent(conn):
    db._maybe_add_column(conn, "t", "new_col", "INTEGER")
    # Second call must not raise.
    db._maybe_add_column(conn, "t", "new_col", "INTEGER")


@pytest.mark.parametrize(
    "bad_name",
    [
        "t; DROP TABLE t; --",
        "t' UNION SELECT 1--",
        "",
        "1startswithdigit",
        "with space",
        "dashy-name",
        "semi;colon",
        "back`tick",
    ],
)
def test_rejects_bad_table_name(conn, bad_name):
    with pytest.raises(ValueError):
        db._maybe_add_column(conn, bad_name, "new_col", "INTEGER")


@pytest.mark.parametrize(
    "bad_col",
    [
        "new_col; DROP TABLE t; --",
        "'; DELETE FROM t; --",
        "",
        "bad-name",
        "with space",
    ],
)
def test_rejects_bad_column_name(conn, bad_col):
    with pytest.raises(ValueError):
        db._maybe_add_column(conn, "t", bad_col, "INTEGER")


@pytest.mark.parametrize(
    "bad_type",
    [
        "INTEGER; DROP TABLE t;",
        'TEXT"; --',
    ],
)
def test_rejects_bad_column_type(conn, bad_type):
    with pytest.raises(ValueError):
        db._maybe_add_column(conn, "t", "new_col", bad_type)


def test_accepts_parametric_column_types(conn):
    # The conservative regex must still accept common DDL forms like
    # `VARCHAR(32)`, `TEXT NOT NULL`, etc.
    conn.execute("CREATE TABLE t2 (id INTEGER PRIMARY KEY)")
    db._maybe_add_column(conn, "t2", "email", "TEXT NOT NULL DEFAULT ''")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(t2)").fetchall()]
    assert "email" in cols
