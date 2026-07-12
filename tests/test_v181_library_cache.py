"""v1.8.1 SPEED-A: hot-cache regression tests for load_workout_library.

The function caches its parsed list at module level. v1.8.1 adds a fast-path
validator (single ``os.stat(WORKOUT_DIR)``) so the hot call no longer iterates
all 3,000+ ZWO files on every invocation. These tests pin that behaviour:

  * First call returns a non-empty list.
  * Second call within the same process returns the *same object* (id match)
    when nothing has changed — proves the cache is being hit, not just
    rebuilt to an equal value.
  * Modifying a file's mtime invalidates the cache (returns a freshly
    built list).
  * Timing: cold call < 5 s, hot call < 100 ms (acceptance from
    MASTER_DECISIONS_v181.md §SPEED-WAVE — "second call returns within 50ms").
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import training_planner as tp


def _reset_cache() -> None:
    """Clear both cache tiers so the next call is a true cold call."""
    tp._WORKOUT_LIB_CACHE.clear()
    tp._WORKOUT_LIB_FAST_VALIDATOR.clear()


@pytest.fixture(autouse=True)
def _isolate_library_caches():
    """3.3.1 (gate-red postmortem): these tests prime tp's cache tiers with a
    SANDBOX 1-row library under an artificially future-bumped mtime and
    previously never cleaned up — monkeypatch restores WORKOUT_DIR, but the
    poisoned tiers survive the test and defeat invalidation for every later
    library consumer in the same xdist worker (test_library_search_v2 then
    saw the 1-row library → empty results for every query; failed ONLY in
    the full parallel gate, never solo — classic order-dependent poison).
    Reset every tier on the way out, including app's request-layer rows
    cache, which had latched rows built from the poisoned tp cache.

    The DECISIVE tier (found by cold-app replication): loading the library
    from the sandbox latches tp._CONTENT_CLASSIFICATION_CACHE as "missing"
    ({} + loaded), because the sandbox has no classification file. Once
    latched it is never re-read — so after the dir reverts, every row's
    content class falls back to filename heuristics and the search's
    family matching returns zero for everything. Reset it to None so the
    next consumer re-reads the REAL classification file."""
    yield
    _reset_cache()
    tp._CONTENT_CLASSIFICATION_CACHE = None
    import app as _app
    with _app._LIBRARY_ROWS_LOCK:
        _app._LIBRARY_ROWS_CACHE = None


def test_first_call_returns_non_empty_library():
    _reset_cache()
    lib = tp.load_workout_library()
    assert isinstance(lib, list)
    assert len(lib) > 0, "expected the workout library to contain at least one ZWO"
    # Shape sanity — each entry must look like a library row.
    sample = lib[0]
    for required in ("Name", "File", "Duration(min)", "Protocol", "Score"):
        assert required in sample, f"library row missing field {required!r}"


def test_second_call_is_identity_match_when_unchanged():
    """The cache must return the SAME list object on a hot call, not just an
    equal one. Identity match is the cheapest signal that no rebuild ran."""
    _reset_cache()
    first = tp.load_workout_library()
    second = tp.load_workout_library()
    assert first is second, (
        "hot call rebuilt the library — cache miss when nothing changed. "
        "Check load_workout_library's fast-path validator."
    )


def test_mtime_change_invalidates_cache(tmp_path, monkeypatch):
    """Pointing WORKOUT_DIR at a fresh dir with one .zwo file, then bumping
    that file's mtime, must invalidate the cache (different object id)."""
    workout_dir = tmp_path / "workouts"
    workout_dir.mkdir()
    zwo = workout_dir / "test_endurance_60min.zwo"
    zwo.write_text(
        '<workout_file>'
        '<author>test</author>'
        '<name>test_endurance_60min</name>'
        '<description>fixture</description>'
        '<sportType>bike</sportType>'
        '<workout>'
        '<SteadyState Duration="3600" Power="0.65"/>'
        '</workout>'
        '</workout_file>',
        encoding="utf-8",
    )

    monkeypatch.setattr(tp, "WORKOUT_DIR", workout_dir)
    _reset_cache()

    first = tp.load_workout_library()
    assert len(first) == 1
    second = tp.load_workout_library()
    assert first is second, "hot call should be cached"

    # Bump mtime ~5 s into the future. Bump both the file AND its parent dir
    # so both cache tiers (fast: dir mtime; slow: per-file mtime) invalidate.
    future = time.time() + 5
    os.utime(zwo, (future, future))
    os.utime(workout_dir, (future, future))

    third = tp.load_workout_library()
    assert third is not first, (
        "cache failed to invalidate after the underlying file mtime advanced"
    )
    assert len(third) == 1  # still finds the same file, just reparsed


def test_cold_call_under_5s_and_hot_call_under_100ms():
    """Acceptance gate from MASTER_DECISIONS_v181.md §SPEED-WAVE:
       hot call must complete in < 50 ms. We test < 100 ms here to leave
       headroom for noisy CI environments."""
    _reset_cache()

    t0 = time.perf_counter()
    lib_cold = tp.load_workout_library()
    cold = time.perf_counter() - t0
    assert lib_cold, "cold call returned empty library"
    assert cold < 5.0, f"cold load too slow: {cold:.3f}s (limit 5s)"

    t1 = time.perf_counter()
    lib_hot = tp.load_workout_library()
    hot = time.perf_counter() - t1
    assert lib_hot is lib_cold
    assert hot < 0.100, (
        f"hot load too slow: {hot*1000:.1f}ms (limit 100ms). "
        "The fast-path validator may not be short-circuiting before the "
        "glob+stat sweep."
    )
