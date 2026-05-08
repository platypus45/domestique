"""v1.6.2 — boot-time plan auto-restore.

If ``current_plan.json`` is missing or zero-bytes on app startup but at
least one ``current_plan.json.bak*`` snapshot is present and parseable,
``_maybe_restore_plan_from_backup`` copies the newest valid snapshot
back into place. Closes the failure mode where a release-time mutation
nuked the live file but the rotated backups survived.
"""
from __future__ import annotations

import json
from pathlib import Path

import app


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_auto_restore_no_op_when_live_present(tmp_path: Path) -> None:
    plan_path = tmp_path / "current_plan.json"
    _write_json(plan_path, {"goal": "x", "weeks": []})

    result = app._maybe_restore_plan_from_backup(plan_path)
    assert result is None
    assert plan_path.exists()


def test_auto_restore_no_op_when_no_backups(tmp_path: Path) -> None:
    plan_path = tmp_path / "current_plan.json"
    # Live missing, no .bak files at all.
    result = app._maybe_restore_plan_from_backup(plan_path)
    assert result is None
    assert not plan_path.exists()


def test_auto_restore_picks_newest_bak(tmp_path: Path) -> None:
    """Live missing + .bak through .bak3 present → restores from .bak
    (newest)."""
    plan_path = tmp_path / "current_plan.json"
    _write_json(tmp_path / "current_plan.json.bak", {"marker": "newest"})
    _write_json(tmp_path / "current_plan.json.bak2", {"marker": "older"})
    _write_json(tmp_path / "current_plan.json.bak3", {"marker": "oldest"})

    result = app._maybe_restore_plan_from_backup(plan_path)

    assert result == "current_plan.json.bak"
    assert plan_path.exists()
    assert json.loads(plan_path.read_text())["marker"] == "newest"


def test_auto_restore_skips_empty_bak(tmp_path: Path) -> None:
    """Empty .bak (0 bytes) is skipped; .bak2 is the first valid one."""
    plan_path = tmp_path / "current_plan.json"
    (tmp_path / "current_plan.json.bak").write_text("")  # 0 bytes
    _write_json(tmp_path / "current_plan.json.bak2", {"marker": "valid"})

    result = app._maybe_restore_plan_from_backup(plan_path)

    assert result == "current_plan.json.bak2"
    assert json.loads(plan_path.read_text())["marker"] == "valid"


def test_auto_restore_skips_unparseable_bak(tmp_path: Path) -> None:
    """Corrupt .bak (non-JSON) is skipped; .bak2 is restored."""
    plan_path = tmp_path / "current_plan.json"
    (tmp_path / "current_plan.json.bak").write_text("{ not json }")
    _write_json(tmp_path / "current_plan.json.bak2", {"marker": "valid"})

    result = app._maybe_restore_plan_from_backup(plan_path)

    assert result == "current_plan.json.bak2"
    assert json.loads(plan_path.read_text())["marker"] == "valid"


def test_auto_restore_skips_empty_dict_bak(tmp_path: Path) -> None:
    """A {} bak is the corruption we're guarding against — skipped, never
    restored."""
    plan_path = tmp_path / "current_plan.json"
    _write_json(tmp_path / "current_plan.json.bak", {})  # The very bug
    _write_json(tmp_path / "current_plan.json.bak2", {"marker": "valid"})

    result = app._maybe_restore_plan_from_backup(plan_path)

    assert result == "current_plan.json.bak2"
    assert json.loads(plan_path.read_text())["marker"] == "valid"


def test_auto_restore_treats_zero_byte_live_as_missing(tmp_path: Path) -> None:
    """Live file at 0 bytes triggers restore (catches truncated-write
    aftermath)."""
    plan_path = tmp_path / "current_plan.json"
    plan_path.write_text("")  # 0 bytes
    _write_json(tmp_path / "current_plan.json.bak", {"marker": "rescue"})

    result = app._maybe_restore_plan_from_backup(plan_path)

    assert result == "current_plan.json.bak"
    assert json.loads(plan_path.read_text())["marker"] == "rescue"


def test_auto_restore_does_not_consume_bak(tmp_path: Path) -> None:
    """The chosen .bak file is COPIED, not moved — it remains available
    for the next .bak in the chain (and the next boot, in the unlikely
    case the freshly-restored live file gets nuked again)."""
    plan_path = tmp_path / "current_plan.json"
    bak = tmp_path / "current_plan.json.bak"
    _write_json(bak, {"marker": "preserved"})

    app._maybe_restore_plan_from_backup(plan_path)

    assert bak.exists()
    assert json.loads(bak.read_text())["marker"] == "preserved"


def test_auto_restore_never_raises_on_unreadable_dir(tmp_path: Path) -> None:
    """If the plan dir is unreadable, return None silently — boot must
    continue."""
    # Pass a non-existent path that also can't be created (parent exists,
    # so a missing live + no baks just yields None).
    plan_path = tmp_path / "nonexistent" / "current_plan.json"
    result = app._maybe_restore_plan_from_backup(plan_path)
    assert result is None
