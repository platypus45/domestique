"""Tests for v1.0.2 startup version-aware migration check.

Covers ``run_v102_migration_check()`` in migrate_profiles.py — the framework
that detects an upgrade by comparing ``~/.domestique/last_run_version.txt``
to the current app version, and returns a result dict shaped per
MASTER_DECISIONS_v102.md §1 that the dashboard consumes via
``GET /api/migrations/last-run-result``.

Three scenarios per MASTER §3:
  (a) fresh install (no last_run_version.txt) → file is written but
      show_toast=False (no upgrade event to surface).
  (b) same-version boot → show_toast=False, version file unchanged.
  (c) upgrade boot (file holds older version) → show_toast=True with
      from_version/to_version populated; subsequent boots no-op.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from migrate_profiles import run_v102_migration_check, _LAST_RUN_VERSION_FILENAME


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """Empty data dir, mirroring the layout of ~/.domestique."""
    d = tmp_path / ".domestique"
    d.mkdir(parents=True, exist_ok=True)
    return d


# (a) Fresh install — no last_run_version.txt yet.
def test_fresh_install_writes_file_but_no_toast(data_dir: Path):
    version_file = data_dir / _LAST_RUN_VERSION_FILENAME
    assert not version_file.exists()

    result = run_v102_migration_check(data_dir, "1.0.2")

    # Locked field-name contract per MASTER §1.
    assert result["migration_check_passed"] is True
    assert result["rider_data_preserved"] is True
    assert result["show_toast"] is False, "fresh install must not toast"
    # First-run from_version mirrors current_version (no prior to compare).
    assert result["to_version"] == "1.0.2"
    assert result["from_version"] == "1.0.2"
    assert result["columns_added"] == 0
    assert result["schema_changes"] == []
    # data_migrations contains the v102_init record.
    ids = [m["id"] for m in result["data_migrations"]]
    assert "v102_init_last_run_version" in ids
    # Persisted version file MUST be created on first boot.
    assert version_file.exists()
    assert version_file.read_text(encoding="utf-8").strip() == "1.0.2"


# (b) Same-version boot — file present, version matches.
def test_same_version_boot_no_toast(data_dir: Path):
    (data_dir / _LAST_RUN_VERSION_FILENAME).write_text("1.0.2", encoding="utf-8")

    result = run_v102_migration_check(data_dir, "1.0.2")

    assert result["migration_check_passed"] is True
    assert result["show_toast"] is False
    assert result["from_version"] == "1.0.2"
    assert result["to_version"] == "1.0.2"
    assert result["columns_added"] == 0
    assert result["rider_data_preserved"] is True
    # Version file still says 1.0.2 (idempotent rewrite ok, content unchanged).
    assert (data_dir / _LAST_RUN_VERSION_FILENAME).read_text(encoding="utf-8").strip() == "1.0.2"


# (c) Upgrade boot — last_run is older than current.
def test_upgrade_boot_toast_fires_once(data_dir: Path):
    (data_dir / _LAST_RUN_VERSION_FILENAME).write_text("1.0.1", encoding="utf-8")

    result = run_v102_migration_check(data_dir, "1.0.2")

    assert result["migration_check_passed"] is True
    assert result["show_toast"] is True, "upgrade boot must surface a toast"
    assert result["from_version"] == "1.0.1"
    assert result["to_version"] == "1.0.2"
    # v1.0.2 ships no schema changes — framework only.
    assert result["columns_added"] == 0
    assert result["schema_changes"] == []
    assert result["rider_data_preserved"] is True
    # File is now bumped to the current version, so the SECOND boot no-ops.
    assert (data_dir / _LAST_RUN_VERSION_FILENAME).read_text(encoding="utf-8").strip() == "1.0.2"

    # Second call simulates the next boot — no toast a second time.
    result2 = run_v102_migration_check(data_dir, "1.0.2")
    assert result2["show_toast"] is False
    assert result2["from_version"] == "1.0.2"
    assert result2["to_version"] == "1.0.2"


# Additional: data_migrations carries the locked id/description/applied keys.
def test_data_migrations_shape(data_dir: Path):
    result = run_v102_migration_check(data_dir, "1.0.2")
    assert isinstance(result["data_migrations"], list)
    assert len(result["data_migrations"]) >= 1
    init = next(m for m in result["data_migrations"] if m["id"] == "v102_init_last_run_version")
    # Locked keys per MASTER §1: id, description, applied.
    assert set(init.keys()) == {"id", "description", "applied"}
    assert init["applied"] is True
    assert isinstance(init["description"], str) and init["description"]
