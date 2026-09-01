"""Loader↔facts parity on rebuild — the 15 formerly-stale index rows.

Investigation verdict (2026-07-07, chip task_05c681c4): the suspected
formula divergence between load_workout_library's closed-form IF/TSS and
the facts parser's 1Hz math DOES NOT EXIST live. Both are RMS-family and
agree within the parity tolerances on all 15 flagged files when the loader
derives rows FRESH (hand-verified: ftp_test_coggan_20min closed-form RMS
0.7960 == facts 0.796; the committed row's 0.75 was a stale legacy value
from before the v2.0.6 exact-ramp-integral fix). The committed index rows
were hand-aligned to facts in cceac6bf; this test pins that a loader
REBUILD of those same rows converges to facts too — so an index self-heal
can never re-introduce the drift. If a real formula change ever lands on
either side, this fails loudly with the file list.
"""
import json
import shutil
import tempfile
from pathlib import Path

import pytest

import training_planner as tp

_FILES = [
    "ftp_test_coggan_3x1min-1min_95pct_59min.zwo", "ftp_test_coggan_3x1min-1min_95pct_59min_v2.zwo",
    "ftp_test_ramp_10w_step_ladder20_152pct_52min.zwo", "neuromuscular_2x3x12s-24s_145pct_120min.zwo",
    "neuromuscular_7x15s_140pct_62min.zwo", "neuromuscular_3x12min_100pct_77min.zwo",
    "over_under_12x3min_95pct_56min_v6.zwo",
    "over_under_12x3min_95pct_56min_v8.zwo",
    "over_under_3x15s_140pct_34min.zwo", "over_under_3x15s_140pct_42min.zwo",
    "over_under_3x4min_90pct_57min.zwo", "recovery_3x295s_90pct_60min.zwo",
    "sprints_5x2min-1min_105pct_59min.zwo", "sprints_7x15s_140pct_60min.zwo",
    "threshold_ladder5_145pct_63min.zwo",
]


def test_loader_rebuild_of_flagged_rows_matches_facts(tmp_path):
    facts_path = Path("src/workouts/.workout_facts.json")
    if not facts_path.exists():
        pytest.skip("facts cache absent")
    facts = json.loads(facts_path.read_text())["facts"]

    # Sandbox: ONLY the 15 files + the classification cache, so the loader
    # parses them fresh (count mismatch → full parse path) without touching
    # the real committed index (repo rule: never rebuild it wholesale).
    for f in _FILES:
        shutil.copy2(f"src/workouts/{f}", tmp_path / f)
    shutil.copy2("src/workouts/.content_classification.json",
                 tmp_path / ".content_classification.json")

    orig = tp.WORKOUT_DIR
    tp.WORKOUT_DIR = tmp_path
    tp._WORKOUT_LIB_CACHE.clear()
    tp._CONTENT_CLASSIFICATION_CACHE = None
    try:
        fresh = {r["File"]: r for r in tp.load_workout_library()}
    finally:
        tp.WORKOUT_DIR = orig
        tp._WORKOUT_LIB_CACHE.clear()
        tp._CONTENT_CLASSIFICATION_CACHE = None

    diverged = []
    for fn in _FILES:
        fr, fa = fresh.get(fn), facts.get(fn)
        assert fr is not None, f"loader produced no row for {fn}"
        assert fa and not fa.get("null"), f"facts row missing/null for {fn}"
        # Same tolerances as test_workout_facts parity (duration always;
        # IF/TSS only on FreeRide-free files — imputation differs by design).
        if abs(fr["Duration(min)"] - fa["dur_s"] / 60.0) > 0.6:
            diverged.append((fn, "dur", fr["Duration(min)"], fa["dur_s"] / 60.0))
        if fa["fr_s"] == 0:
            if abs(float(fr["IF"]) - fa["if"]) > 0.02:
                diverged.append((fn, "if", fr["IF"], fa["if"]))
            t = fa["tss"]
            if t and abs(float(fr["TSS"]) - t) > max(2.0, 0.03 * t):
                diverged.append((fn, "tss", fr["TSS"], t))
    assert diverged == [], (
        "loader rebuild diverges from facts — a REAL formula change landed "
        f"on one side: {diverged}")
