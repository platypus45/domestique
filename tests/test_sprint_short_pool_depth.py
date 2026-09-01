"""v3.4.0 W4 (continuous mode, P6 amendment F) — short sprint pool depth.

Continuous mode rotates ≥1 sprint/neuromuscular exposure per week
indefinitely, so the servable sprint pool must stay deep enough at SHORT
durations that a time-crunched rider (hard weekday slot ≤45min) never
cycles the same few files. Pre-3.4.0 the library had exactly 3 servable
sprint files in the 30-45min bucket and 0 below 30 — the W4 authoring wave
added ten 33-44min neuromuscular sessions to fix that.

"Servable" = the production serve path: content class in the sprint slot's
class set, class-aware Score floor, and tp.file_admissible('sprint', row)
(IF ≤ 0.82 ∧ t150 ≥ 60s ∧ sprints ≥ 4 against the facts cache).

Floors (locked by the P6 grill verdict): ≥8 servable in the 30-45min
bucket, ≥5 servable at 25-35min.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import training_planner as tp  # noqa: E402
import workout_facts as wf  # noqa: E402

WK = ROOT / "src" / "workouts"
pytestmark = pytest.mark.skipif(
    not (WK / wf.FACTS_FILENAME).exists(), reason="facts cache absent")

SPRINT_CLASSES = {"neuromuscular", "anaerobic", "sprint"}


@pytest.fixture(scope="module")
def servable_sprint_rows():
    """Sprint-slot servable rows from the runtime library view. Restores the
    index afterwards so a self-heal write can't leak into the working tree
    (same pattern as test_slot_contracts)."""
    backup = (WK / ".library_index.json").read_bytes()
    wf.reset_cache()
    tp._WORKOUT_LIB_CACHE.clear()
    tp._WORKOUT_LIB_FAST_VALIDATOR.clear()
    tp._CONTENT_CLASSIFICATION_CACHE = None
    try:
        rows = tp.load_workout_library()
        out = []
        for r in rows:
            cc = tp._content_class_for_row(r)
            if cc not in SPRINT_CLASSES:
                continue
            if float(r.get("Score") or 0) < tp._class_aware_score_floor(cc):
                continue
            if not tp.file_admissible("sprint", r):
                continue
            out.append(r)
        yield out
    finally:
        if (WK / ".library_index.json").read_bytes() != backup:
            (WK / ".library_index.json").write_bytes(backup)
        wf.reset_cache()


def _dur(r) -> float:
    return float(r.get("Duration(min)") or 0)


def test_sprint_pool_depth_30_45_bucket(servable_sprint_rows):
    """≥8 servable sprint files in the 30-45min bucket (was 3 pre-3.4.0)."""
    n = sum(1 for r in servable_sprint_rows if 30.0 <= _dur(r) < 45.0)
    assert n >= 8, f"sprint pool 30-45min bucket too thin: {n} < 8"


def test_sprint_pool_depth_25_35_range(servable_sprint_rows):
    """≥5 servable sprint files at 25-35min (was 1 pre-3.4.0)."""
    n = sum(1 for r in servable_sprint_rows if 25.0 <= _dur(r) <= 35.0)
    assert n >= 5, f"sprint pool 25-35min range too thin: {n} < 5"


def test_w4_short_sprint_files_are_servable():
    """The ten W4-authored files exist, classify neuromuscular, clear the
    HIT Score floor, and are admissible to the sprint slot — i.e. actually
    reachable by the serve path, not just present on disk."""
    w4_files = [
        "neuromuscular_4x15s-3min_220pct_33min.zwo",
        "neuromuscular_4x16s-3min_225pct_33min.zwo",
        "neuromuscular_4x18s-3min_205pct_33min.zwo",
        "neuromuscular_4x20s-3min_195pct_34min.zwo",
        "neuromuscular_4x15s_230pct_34min.zwo",
        "neuromuscular_5x45s_78pct_40min.zwo",
        "neuromuscular_2x12s-3min_240pct_41min.zwo",
        "neuromuscular_7x10s-3min_250pct_43min.zwo",
        "neuromuscular_7x12s-3min_210pct_43min.zwo",
        "neuromuscular_6x15s-3min_215pct_44min.zwo",
    ]
    missing = [fn for fn in w4_files if not (WK / fn).exists()]
    assert missing == [], f"W4 files missing from workouts/: {missing}"
    backup = (WK / ".library_index.json").read_bytes()
    wf.reset_cache()
    tp._WORKOUT_LIB_CACHE.clear()
    tp._WORKOUT_LIB_FAST_VALIDATOR.clear()
    tp._CONTENT_CLASSIFICATION_CACHE = None
    try:
        by_file = {r["File"]: r for r in tp.load_workout_library()}
        bad = []
        for fn in w4_files:
            r = by_file.get(fn)
            if r is None:
                bad.append((fn, "no index row"))
                continue
            cc = tp._content_class_for_row(r)
            if cc not in SPRINT_CLASSES:
                bad.append((fn, f"class={cc}"))
            elif float(r.get("Score") or 0) < tp._class_aware_score_floor(cc):
                bad.append((fn, f"score={r.get('Score')}"))
            elif not tp.file_admissible("sprint", r):
                bad.append((fn, "inadmissible"))
        assert bad == [], f"W4 sprint files not servable: {bad}"
    finally:
        if (WK / ".library_index.json").read_bytes() != backup:
            (WK / ".library_index.json").write_bytes(backup)
        wf.reset_cache()
