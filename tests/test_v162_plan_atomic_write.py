"""v1.6.2 — atomic_write_plan + _rotate_plan_backups crash-safety tests.

Closes the bug where ``current_plan.json`` could be wiped between v1.3.x
and v1.4.x ships. The new ``atomic_write_plan`` helper:

* Refuses to write empty / non-dict plans (so a mutation that produced
  ``{}`` or ``None`` cannot silently nuke contents).
* Rotates the live file → ``.bak`` → ``.bak2`` → ... → ``.bak7`` BEFORE
  writing, so a crash mid-write still leaves up to 7 prior snapshots.
* Writes via tmp + atomic ``Path.replace`` — partial writes never
  surface as the live file.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import training_planner as tp


def _make_valid_plan(marker: str = "v1") -> dict:
    return {
        "goal": "general",
        "weeks": [{"week_num": 1, "sessions": []}],
        "marker": marker,
    }


def test_atomic_write_plan_rejects_empty_dict(tmp_path: Path) -> None:
    """Refuses to write ``{}`` — preserves live file."""
    plan_path = tmp_path / "current_plan.json"
    plan_path.write_text(json.dumps(_make_valid_plan("orig")))

    with pytest.raises(ValueError):
        tp.atomic_write_plan(plan_path, {})

    # Live file unchanged.
    loaded = json.loads(plan_path.read_text())
    assert loaded["marker"] == "orig"


def test_atomic_write_plan_rejects_none(tmp_path: Path) -> None:
    """Refuses non-dict input — preserves live file."""
    plan_path = tmp_path / "current_plan.json"
    plan_path.write_text(json.dumps(_make_valid_plan("orig")))

    with pytest.raises(ValueError):
        tp.atomic_write_plan(plan_path, None)  # type: ignore[arg-type]

    loaded = json.loads(plan_path.read_text())
    assert loaded["marker"] == "orig"


def test_atomic_write_plan_crash_mid_write_preserves_live(tmp_path: Path) -> None:
    """If json.dump raises, the live file is unchanged and tmp may exist
    but is never renamed over the live."""
    plan_path = tmp_path / "current_plan.json"
    plan_path.write_text(json.dumps(_make_valid_plan("orig")))

    new_plan = _make_valid_plan("crashed")

    def boom(*args, **kwargs):
        raise IOError("simulated disk full")

    with patch("training_planner.json.dump", side_effect=boom):
        with pytest.raises(IOError):
            tp.atomic_write_plan(plan_path, new_plan)

    # Live file still has the original contents.
    loaded = json.loads(plan_path.read_text())
    assert loaded["marker"] == "orig"


def test_atomic_write_plan_rotates_backups(tmp_path: Path) -> None:
    """First write with no live → no rotation. Second write rotates
    live→.bak. Third write rotates .bak→.bak2 and live→.bak."""
    plan_path = tmp_path / "current_plan.json"
    bak1 = tmp_path / "current_plan.json.bak"
    bak2 = tmp_path / "current_plan.json.bak2"

    # First write (no prior live → no rotation).
    tp.atomic_write_plan(plan_path, _make_valid_plan("v1"))
    assert plan_path.exists()
    assert not bak1.exists()

    # Second write rotates v1 → .bak.
    tp.atomic_write_plan(plan_path, _make_valid_plan("v2"))
    assert plan_path.exists() and json.loads(plan_path.read_text())["marker"] == "v2"
    assert bak1.exists() and json.loads(bak1.read_text())["marker"] == "v1"

    # Third write: .bak → .bak2, then live → .bak.
    tp.atomic_write_plan(plan_path, _make_valid_plan("v3"))
    assert json.loads(plan_path.read_text())["marker"] == "v3"
    assert json.loads(bak1.read_text())["marker"] == "v2"
    assert bak2.exists() and json.loads(bak2.read_text())["marker"] == "v1"


def test_atomic_write_plan_rotation_caps_at_depth(tmp_path: Path) -> None:
    """After PLAN_BACKUP_DEPTH+1 writes, only .bak through .bak{depth}
    exist; the oldest is dropped on the next rotation."""
    plan_path = tmp_path / "current_plan.json"

    # Write PLAN_BACKUP_DEPTH+2 versions so rotation fully fills + drops.
    n_writes = tp.PLAN_BACKUP_DEPTH + 2
    for i in range(1, n_writes + 1):
        tp.atomic_write_plan(plan_path, _make_valid_plan(f"v{i}"))

    # Live file has the latest.
    assert json.loads(plan_path.read_text())["marker"] == f"v{n_writes}"

    # .bak through .bak{depth} all exist.
    for n in range(1, tp.PLAN_BACKUP_DEPTH + 1):
        suffix = ".bak" if n == 1 else f".bak{n}"
        bak = tmp_path / f"current_plan.json{suffix}"
        assert bak.exists(), f"{bak.name} should exist after {n_writes} writes"

    # No file beyond .bak{depth}.
    overflow = tmp_path / f"current_plan.json.bak{tp.PLAN_BACKUP_DEPTH + 1}"
    assert not overflow.exists()


def test_plan_write_lock_is_reentrant(tmp_path: Path) -> None:
    """Lock must be reentrant so callers already inside ``plan_write_lock``
    can call ``atomic_write_plan`` (which takes the lock internally)
    without deadlocking. Critical for the auto-reforecast site that
    re-reads + writes inside one outer lock block."""
    plan_path = tmp_path / "current_plan.json"

    with tp.plan_write_lock():
        # Should not deadlock.
        tp.atomic_write_plan(plan_path, _make_valid_plan("nested"))

    assert json.loads(plan_path.read_text())["marker"] == "nested"
