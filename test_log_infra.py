"""Tests for Domestique's backend observability layer.

v4.0.0-alpha scope: trainer subsystem removed. This suite no longer tests
the deleted `.ble` / `.gate` / `.phase` / `.trainer` / `.hr` / `.session`
/ `.ws` category loggers; those tests were deleted with the trainer rip.
What survives:

  - `setup_logging()` default + DOMESTIQUE_VERBOSE env handling.
  - `get_levels()` covers every category the app declares (whatever those
    are post-rip; we assert via `log_config.CATEGORY_NAMES`).
  - `set_level()` root + category + FQDN + error paths.
  - Per-session log file creation under a temp HOME.
  - Rotation honours `DOMESTIQUE_RIDE_LOG_KEEP`.
  - `GET|POST /api/training/log-level` runtime toggle.

All tests point LOG_DIR at a tmp path so nobody's real ~/.domestique is
touched. Each test that reconfigures logging resets `_configured` to
False before asserting so the setup path actually runs.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _reset_log_config(log_dir: Path):
    """Re-import log_config with LOG_DIR pointed at a fresh tmp path.

    Also detaches every non-stderr handler that a prior test left
    attached to the root logger, so our assertions about handler
    counts are stable.
    """
    import log_config

    # Tear down any existing ride handlers from a prior test iteration.
    for sid in list(log_config._session_handlers.keys()):
        log_config.stop_session_log(sid)
    log_config.stop_flusher(join_timeout=0.5)
    # Scrub root.handlers so the next setup_logging() starts clean.
    root = logging.getLogger()
    for h in list(root.handlers):
        try:
            h.close()
        except Exception:
            pass
        root.removeHandler(h)

    log_config._configured = False
    log_config.LOG_DIR = log_dir
    log_config.LOG_FILE = log_dir / "domestique.log"
    # Replay the module-level lazy initializer — `_session_handlers` etc.
    log_config._session_handlers.clear()
    log_config._session_paths.clear()
    log_config._flusher_stop.clear()
    return log_config


@pytest.fixture
def tmp_log_home(tmp_path, monkeypatch):
    """Yield a fresh log_config with LOG_DIR at tmp_path/logs."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Clear env flags so tests don't inherit the real shell
    for var in (
        "DOMESTIQUE_VERBOSE",
        "DOMESTIQUE_LOG_CATEGORIES",
        "DOMESTIQUE_RIDE_LOG_KEEP",
    ):
        monkeypatch.delenv(var, raising=False)
    yield _reset_log_config(log_dir)
    # Tidy after the test
    import log_config
    for sid in list(log_config._session_handlers.keys()):
        log_config.stop_session_log(sid)
    log_config.stop_flusher(join_timeout=0.5)


# ──────────────────────────────────────────────────────────────────────
# setup_logging / env handling
# ──────────────────────────────────────────────────────────────────────


def test_setup_logging_default_info(tmp_log_home):
    lc = tmp_log_home
    lc.setup_logging()
    assert logging.getLogger().getEffectiveLevel() == logging.INFO


def test_setup_logging_verbose_env(tmp_log_home, monkeypatch):
    lc = tmp_log_home
    monkeypatch.setenv("DOMESTIQUE_VERBOSE", "1")
    lc._configured = False
    lc.setup_logging()
    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG


def test_setup_logging_categories_env_ignores_unknown(tmp_log_home, monkeypatch):
    """Unknown category names must be ignored without raising."""
    lc = tmp_log_home
    monkeypatch.setenv(
        "DOMESTIQUE_LOG_CATEGORIES", "bogus,not-a-real-cat",
    )
    lc._configured = False
    lc.setup_logging()  # must not raise


# ──────────────────────────────────────────────────────────────────────
# get_levels / set_level
# ──────────────────────────────────────────────────────────────────────


def test_get_levels_covers_all_categories(tmp_log_home):
    """Every category declared in CATEGORY_NAMES appears in get_levels()."""
    lc = tmp_log_home
    levels = lc.get_levels()
    assert "root" in levels
    for cat in lc.CATEGORY_NAMES:
        assert f"domestique.{cat}" in levels


def test_set_level_root(tmp_log_home):
    lc = tmp_log_home
    lc.set_level("DEBUG")
    assert logging.getLogger().level == logging.DEBUG
    lc.set_level("INFO")
    assert logging.getLogger().level == logging.INFO


def test_set_level_category_short_name(tmp_log_home):
    """Setting level by short category name routes to domestique.<cat>.

    Uses whatever the first declared category happens to be post-rip
    (the test is category-name agnostic so it survives future trims).
    """
    lc = tmp_log_home
    if not lc.CATEGORY_NAMES:
        pytest.skip("log_config declares no category loggers")
    cat = lc.CATEGORY_NAMES[0]
    resolved = lc.set_level("DEBUG", category=cat)
    assert resolved == "DEBUG"
    assert logging.getLogger(f"domestique.{cat}").level == logging.DEBUG


def test_set_level_category_fqdn(tmp_log_home):
    """Fully-qualified domain-name form (domestique.<cat>) also works."""
    lc = tmp_log_home
    if not lc.CATEGORY_NAMES:
        pytest.skip("log_config declares no category loggers")
    cat = lc.CATEGORY_NAMES[0]
    lc.set_level("WARNING", category=f"domestique.{cat}")
    assert logging.getLogger(f"domestique.{cat}").level == logging.WARNING


def test_set_level_unknown_category_raises(tmp_log_home):
    lc = tmp_log_home
    with pytest.raises(ValueError):
        lc.set_level("DEBUG", category="not-a-real-cat")


def test_set_level_unknown_level_raises(tmp_log_home):
    lc = tmp_log_home
    with pytest.raises(ValueError):
        lc.set_level("LOUD")


# ──────────────────────────────────────────────────────────────────────
# Per-session ride logs — creation + rotation
# ──────────────────────────────────────────────────────────────────────


def test_start_session_log_creates_file(tmp_log_home):
    lc = tmp_log_home
    sid = lc.start_session_log("test01")
    assert sid == "test01"
    # Exactly one session file should exist after a fresh start.
    # v4.0.0-alpha: filename pattern moved from ride_* to app_* because
    # there is no ride-session concept in-app anymore.
    ride_files = sorted(lc.LOG_DIR.glob("app_*.log"))
    assert len(ride_files) == 1
    assert "test01" in ride_files[0].name
    # Path is retrievable.
    assert lc.get_active_log_path(sid) == str(ride_files[0])
    lc.stop_session_log(sid)


def test_session_log_rotation_keeps_configured_count(tmp_log_home, monkeypatch):
    lc = tmp_log_home
    monkeypatch.setenv("DOMESTIQUE_RIDE_LOG_KEEP", "3")
    # Create 5 sessions back-to-back. Only 3 files should survive on disk.
    for i in range(5):
        sid = lc.start_session_log(f"ride{i:02}")
        lc.stop_session_log(sid)
    # Each start calls _prune_old_app_logs(keep-1) before opening; after
    # the 5th start we have at most `keep` files.
    files = list(lc.LOG_DIR.glob("app_*.log"))
    assert len(files) <= 3


def test_stop_session_log_is_idempotent(tmp_log_home):
    lc = tmp_log_home
    sid = lc.start_session_log("idem01")
    lc.stop_session_log(sid)
    lc.stop_session_log(sid)  # must not raise


def test_get_active_log_path_none_when_no_session(tmp_log_home):
    lc = tmp_log_home
    assert lc.get_active_log_path() is None
    assert lc.get_active_log_path("nonexistent") is None


def test_get_active_log_path_single_session(tmp_log_home):
    lc = tmp_log_home
    sid = lc.start_session_log("solo01")
    try:
        # No arg — works because exactly one is active.
        assert lc.get_active_log_path() is not None
        assert lc.get_active_log_path(sid) is not None
    finally:
        lc.stop_session_log(sid)


# HTTP endpoints — `/api/training/log-level` removed with the trainer rip
# (v4.0.0-alpha). Runtime logger level changes are now only via env vars
# or `log_config.set_level()` in-process.
