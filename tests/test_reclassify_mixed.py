"""Wave 1A RECLASSIFY-MIXED-v461 tests.

Verifies the v4.6.1 promotion-from-mixed behavior:
  1. pattern_microinterval + Z5 ≥ 6 → vo2_short
  2. pattern_microinterval + Z6 ≥ 1 → anaerobic
  3. has_sprints + Z7 ≥ 0.5 → neuromuscular
  4. cycle_period 30-90s + 10+ reps at 95-115% FTP → tagged is_ronnestad
  5. After full re-run: anaerobic > 200, neuromuscular > 30, vo2_short > 100
  6. After full re-run: mixed < 250
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "scripts"))

from reclassify_mixed_v461 import (  # noqa: E402
    detect_ronnestad,
    promote_mixed,
)

WORKOUTS_DIR = ROOT / "src" / "workouts"
CACHE_PATH = WORKOUTS_DIR / ".content_classification.json"


@pytest.fixture(scope="module")
def classifications() -> dict:
    with CACHE_PATH.open() as f:
        return json.load(f)["classifications"]


# ── Promotion-rule unit tests ────────────────────────────────────────────────


def _make_entry(*, flags: dict | None = None, dur_s: int = 3600,
                z1: float = 0, z2: float = 0,
                z3: float = 0, z4: float = 0, z5: float = 0, z6: float = 0,
                z7: float = 0, ss: float = 0,
                ou_transitions: int = 0) -> dict:
    """Synthesize an entry shaped like the cached classifier output."""
    return {
        "primary": "mixed",
        "secondary_flags": flags or {},
        "features": {
            "duration_s": dur_s,
            "valid_dur_s": dur_s,
            "z1_pct": z1,
            "z2_pct": z2,
            "z3_pct": z3,
            "z4_pct": z4,
            "z5_pct": z5,
            "z6_pct": z6,
            "z7_pct": z7,
            "sweet_spot_pct": ss,
            "ou_transitions": ou_transitions,
        },
    }


def test_microinterval_z5_dose_promotes_to_vo2_short():
    # 60min × 12% Z5 = 7.2min → > 5min vo2max threshold + microinterval flag
    entry = _make_entry(flags={"pattern_microinterval": True}, dur_s=3600,
                        z5=12)
    assert promote_mixed(entry) == "vo2_short"


def test_microinterval_z6_dose_promotes_to_anaerobic():
    # 60min × 4% Z6 = 2.4min → > 1min Z6 threshold for microinterval+anaerobic
    entry = _make_entry(flags={"pattern_microinterval": True}, dur_s=3600,
                        z6=4)
    assert promote_mixed(entry) == "anaerobic"


def test_sprints_z7_dose_promotes_to_neuromuscular():
    # 30min × 3% Z7 = 0.9min → > 0.5min threshold
    entry = _make_entry(flags={"has_sprints": True}, dur_s=1800, z7=3)
    assert promote_mixed(entry) == "neuromuscular"


def test_pattern_over_under_promotes_to_over_under():
    entry = _make_entry(flags={"pattern_over_under": True}, dur_s=3600, z4=10,
                        ou_transitions=5)
    assert promote_mixed(entry) == "over_under"


def test_no_signal_stays_mixed():
    """Bare entry with no flag and no zone-time signature stays mixed.

    This entry has tiny Z3/Z4/Z5 doses (sub-threshold) and is too short to
    trigger the recovery/endurance fallback (Z1+Z2 is 0% here).
    """
    entry = _make_entry(dur_s=600, z3=2, z4=1, z5=1)
    assert promote_mixed(entry) == "mixed"


def test_recovery_prefix_stays_safe():
    """A `recovery_*.zwo` file with hard zone time should NOT be promoted to
    threshold/vo2max — test_recovery_prefix_consistent requires it to stay
    in {recovery, mixed}.
    """
    entry = _make_entry(flags={"has_threshold_work": True}, dur_s=3600, z4=20)
    # Without filename hint: would go to threshold
    assert promote_mixed(entry) == "threshold"
    # With recovery_* filename: must stay safe
    assert promote_mixed(entry, filename="recovery_ladder6_109pct_63min.zwo") == "mixed"


# ── Rønnestad-detection tests ────────────────────────────────────────────────


def test_ronnestad_30_15_detected_in_real_file():
    """A known Rønnestad-style 30/15 file in the spec band (95-115% FTP)."""
    fn = WORKOUTS_DIR / "over_under_4x3min_92pct_65min.zwo"
    if not fn.exists():
        pytest.skip(f"{fn.name} not in library")
    ronn = detect_ronnestad(fn)
    assert ronn is not None
    assert ronn["protocol"] == "30/15"
    assert ronn["reps"] >= 10
    assert 0.95 <= ronn["on_p"] <= 1.20


def test_ronnestad_40_20_detected_in_real_file():
    """vo2_short_2x12x40s-20s_110pct_60min.zwo is a known Rønnestad 40/20 file."""
    fn = WORKOUTS_DIR / "vo2_short_2x12x40s-20s_110pct_60min.zwo"
    if not fn.exists():
        pytest.skip(f"{fn.name} not in library")
    ronn = detect_ronnestad(fn)
    assert ronn is not None
    assert ronn["protocol"] == "40/20"
    assert ronn["reps"] >= 10
    assert 0.95 <= ronn["on_p"] <= 1.20


def test_ronnestad_rejects_non_microinterval():
    """A file with no IntervalsT block returns None."""
    # Find any anaerobic_steady_*.zwo (no IntervalsT)
    candidates = [p for p in WORKOUTS_DIR.glob("anaerobic_steady_*.zwo")]
    if not candidates:
        pytest.skip("no steady-state files to probe")
    ronn = detect_ronnestad(candidates[0])
    # Steady-state can never have a Rønnestad block
    assert ronn is None


# ── Full-library post-run integration tests ─────────────────────────────────


@pytest.mark.xfail(
    reason="v1.0.4 IMPL-CLASSIFIER: post-run distribution targets came from "
           "the legacy `reclassify_mixed_v461.py` script. The v1.0.4 "
           "structural rewrite supersedes that script — `mixed` is dropped, "
           "`vo2_short` count is content-driven (no inflation pass), and the "
           "split between vo2_short / vo2_ladder / anaerobic moved.",
    strict=False,
)
def test_post_run_class_distribution(classifications):
    """After scripts/reclassify_mixed_v461.py runs, the class distribution
    should hit the spec gates:
      vo2_short > 100, anaerobic > 200, neuromuscular > 30, mixed < 250.
    """
    counts = Counter(v.get("primary", "?") for v in classifications.values())
    assert counts["vo2_short"] > 100, (
        f"vo2_short = {counts['vo2_short']} (need > 100)"
    )
    assert counts["anaerobic"] > 200, (
        f"anaerobic = {counts['anaerobic']} (need > 200)"
    )
    assert counts["neuromuscular"] > 30, (
        f"neuromuscular = {counts['neuromuscular']} (need > 30)"
    )
    assert counts["mixed"] < 250, (
        f"mixed = {counts['mixed']} (need < 250)"
    )


def test_post_run_total_unchanged(classifications):
    """Reclassification only touches `primary`; it never adds or removes files.
    v1.10.0: assert the cache covers EXACTLY the library (1:1, no orphans/ghosts)
    instead of a frozen 3054 count — stale since the library grew."""
    n_files = len(list(WORKOUTS_DIR.glob("*.zwo")))
    assert len(classifications) == n_files


def test_ronnestad_files_tagged(classifications):
    """At least the known Rønnestad-named files are tagged is_ronnestad."""
    expected = [
        "vo2_short_13x30s-15s_120pct_64min.zwo",
        "vo2_short_2x12x40s-20s_110pct_60min.zwo",
    ]
    tagged = 0
    for fn in expected:
        entry = classifications.get(fn, {})
        if "is_ronnestad" in entry.get("tags", []):
            tagged += 1
    assert tagged >= 1, (
        f"No Rønnestad-style files tagged after re-run; expected ≥1 of "
        f"{expected}"
    )
