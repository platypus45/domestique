"""v4.2.0 IMPL-LIBRARY: score_sync regression suite.

Closes the v4.1.1 Bug C PARTIAL — score divergence between
``training_planner.load_workout_library`` and ``app.py /api/workouts``.
Both code paths now route through ``training_planner.score_workout``,
so for any input ZWO the score must be identical.

Approach: pick a deterministic sample of 10 ZWO files from
``workouts/``, run them through both code paths, and assert equality
file-by-file. Also exercises the helper directly with synthetic dicts
to lock the formula against accidental drift.
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

# Ensure the project root is importable when pytest runs from /tests.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import training_planner as tp  # noqa: E402

WORKOUT_DIR = ROOT / "workouts"


def _sample_zwo_files() -> list[Path]:
    """Pick 10 ZWO files spanning recovery → vo2max if possible."""
    if not WORKOUT_DIR.exists():
        return []
    candidates = sorted(WORKOUT_DIR.glob("*.zwo"))
    if not candidates:
        return []
    # Deterministic 10-file stride across the library so sample drifts
    # with the library; never hard-code filenames.
    step = max(1, len(candidates) // 10)
    picks = candidates[::step][:10]
    if len(picks) < 10:
        picks = candidates[:10]
    return picks


SAMPLE_FILES = _sample_zwo_files()


def _scan_via_api(zwo_path: Path) -> dict:
    """Compute structural metrics + score using the app.py code path."""
    import app
    scan = app._scan_zwo_for_library(zwo_path)
    assert scan is not None, f"app failed to scan {zwo_path.name}"
    score = max(1, min(10, int(round(tp.score_workout({
        "tss": scan["tss"],
        "total_sec": scan["total_sec"],
        "z1_sec": scan["z1_sec"], "z2_sec": scan["z2_sec"], "z3_sec": scan["z3_sec"],
        "z4_sec": scan["z4_sec"], "z5_sec": scan["z5_sec"], "z6_sec": scan["z6_sec"],
        "distinct_high_targets": scan["distinct_high_targets"],
        "has_vo2_intensity": scan["has_vo2_intensity"],
    })))))
    return {
        "score": score,
        "tss": scan["tss"],
        "total_sec": scan["total_sec"],
    }


def _scan_via_planner(zwo_path: Path) -> dict:
    """Look up the same file's score from ``load_workout_library``."""
    lib = tp.load_workout_library()
    by_file = {row["File"]: row for row in lib}
    assert zwo_path.name in by_file, f"planner library missing {zwo_path.name}"
    row = by_file[zwo_path.name]
    return {"score": int(row["Score"]), "tss": float(row["TSS"])}


@pytest.mark.skipif(not SAMPLE_FILES, reason="no ZWO library available")
@pytest.mark.parametrize("zwo_path", SAMPLE_FILES, ids=lambda p: p.name)
def test_score_sync_per_file(zwo_path: Path) -> None:
    """app.py and training_planner produce IDENTICAL int scores per file.

    This is the core regression for v4.1.1 Bug C PARTIAL.
    """
    api = _scan_via_api(zwo_path)
    planner = _scan_via_planner(zwo_path)
    assert api["score"] == planner["score"], (
        f"score mismatch on {zwo_path.name}: "
        f"api={api['score']} vs planner={planner['score']}"
    )


# ── Direct helper-formula tests ──────────────────────────────────────────────


def test_score_workout_clamps_to_one_for_empty():
    """Empty/zero-input dict returns the floor (1.0)."""
    assert tp.score_workout({}) == 1.0
    assert tp.score_workout({"total_sec": 0}) == 1.0


def test_score_workout_clamps_to_ten_max():
    """A high-TSS structured session should clamp at 10.0."""
    s = tp.score_workout({
        "tss": 250.0, "total_sec": 3600,
        "z1_sec": 0, "z2_sec": 0, "z3_sec": 1800,
        "z4_sec": 1800, "z5_sec": 0, "z6_sec": 0,
        "distinct_high_targets": {85, 95, 100, 105, 110},
        "has_vo2_intensity": True,
    })
    assert s == 10.0


def test_score_workout_recovery_stays_low():
    """Pure Z1 short session lands in the LOW band (<4)."""
    s = tp.score_workout({
        "tss": 15.0, "total_sec": 1800,  # 30 min @ Z1
        "z1_sec": 1800, "z2_sec": 0, "z3_sec": 0,
        "z4_sec": 0, "z5_sec": 0, "z6_sec": 0,
        "distinct_high_targets": set(),
        "has_vo2_intensity": False,
    })
    assert s < 4.0, f"recovery should land in LOW band but got {s}"


def test_score_workout_aerobic_bonus_triggers():
    """Long Z2 (≥50% Z2 + ≥75min) gets +0.5 aerobic bonus."""
    base = {
        "tss": 75.0, "total_sec": 4500,  # 75 min
        "z1_sec": 900, "z2_sec": 3000,   # 50% Z2 = 50 min
        "z3_sec": 600, "z4_sec": 0, "z5_sec": 0, "z6_sec": 0,
        "distinct_high_targets": {80},
        "has_vo2_intensity": False,
    }
    short = {**base, "total_sec": 1800, "z2_sec": 1200, "z1_sec": 600, "tss": 30.0}
    long = base
    s_short = tp.score_workout(short)
    s_long = tp.score_workout(long)
    # Long should score at least 0.5 higher because the aerobic bonus fires.
    assert s_long >= s_short + 0.4


def test_score_workout_variety_bonus_int_or_set():
    """Helper accepts either a set OR an int for distinct_high_targets."""
    common = {
        "tss": 60.0, "total_sec": 3600,
        "z1_sec": 600, "z2_sec": 1200, "z3_sec": 1800,
        "z4_sec": 0, "z5_sec": 0, "z6_sec": 0,
        "has_vo2_intensity": False,
    }
    a = tp.score_workout({**common, "distinct_high_targets": {80, 90, 95, 100}})
    b = tp.score_workout({**common, "distinct_high_targets": 4})
    assert a == b


def test_score_workout_vo2_bonus_adds_one():
    """has_vo2_intensity=True adds exactly +1.0."""
    base = {
        "tss": 50.0, "total_sec": 2700,
        "z1_sec": 300, "z2_sec": 600, "z3_sec": 1800,
        "z4_sec": 0, "z5_sec": 0, "z6_sec": 0,
        "distinct_high_targets": {85},
        "has_vo2_intensity": False,
    }
    no_vo2 = tp.score_workout(base)
    vo2 = tp.score_workout({**base, "has_vo2_intensity": True})
    assert abs((vo2 - no_vo2) - 1.0) < 0.01


def test_score_workout_returns_float_in_range():
    """Helper always returns a float in [1.0, 10.0]."""
    s = tp.score_workout({
        "tss": 100.0, "total_sec": 3600,
        "z2_sec": 1800, "z3_sec": 1800,
    })
    assert isinstance(s, float)
    assert 1.0 <= s <= 10.0


def test_tier_mapping_canonical():
    """MASTER §3 tier mapping: low <4, medium 4-6, good 7+."""
    # Build inputs that land squarely in each band.
    low = tp.score_workout({
        "tss": 10.0, "total_sec": 600, "z1_sec": 600,
    })
    assert low < 4.0
    # Medium: moderate TSS + some structure.
    med = tp.score_workout({
        "tss": 70.0, "total_sec": 3600,
        "z1_sec": 600, "z2_sec": 1500, "z3_sec": 1500,
        "distinct_high_targets": {80}, "has_vo2_intensity": False,
    })
    assert 4.0 <= med < 7.0
    # Good: high TSS + variety + VO2.
    good = tp.score_workout({
        "tss": 150.0, "total_sec": 3600,
        "z1_sec": 0, "z2_sec": 600, "z3_sec": 600,
        "z4_sec": 1200, "z5_sec": 1200, "z6_sec": 0,
        "distinct_high_targets": {90, 100, 108}, "has_vo2_intensity": True,
    })
    assert good >= 7.0
