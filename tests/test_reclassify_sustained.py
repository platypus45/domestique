"""Regression guard for the v2.4.4 sustained-hard reclassification.

Background: ``classify_v104`` routes a workout whose hard main-set falls just
under every strict dose gate (8-min VO2, 15-min threshold, 18-min over-under
band) to a zone-dominance fallback that ignores cumulative hard work — so real
threshold/VO2/anaerobic sessions (a 6×2 min set, a Billat, 3×3 min) landed on
``endurance``/``recovery`` and could be served on easy days. scripts/
reclassify_sustained.py corrected a hand-verified set (two independent
classification passes, reconciled) in both caches.

These tests lock that correction so a future edit / partial re-run can't silently
revert it, and re-assert the library invariants the correction had to respect.
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WK = ROOT / "workouts"
LABELS = ROOT / "scripts" / "reclassify_sustained_labels.json"
CC_PATH = WK / ".content_classification.json"
IDX_PATH = WK / ".library_index.json"

EASY = {"endurance", "recovery", "endurance_intervals"}


@pytest.fixture(scope="module")
def classifications():
    return json.loads(CC_PATH.read_text())["classifications"]


@pytest.fixture(scope="module")
def index_rows():
    return {r["File"]: r for r in json.loads(IDX_PATH.read_text())["rows"]}


@pytest.fixture(scope="module")
def labels():
    return json.loads(LABELS.read_text())


def test_sustained_files_carry_their_corrected_hard_class(classifications, labels):
    """Every reclassified file keeps its verified hard type — not endurance/
    recovery (the mislabel) and not the forbidden ``mixed``."""
    bad = []
    for fn, want in labels.items():
        got = classifications.get(fn, {}).get("primary")
        if got != want:
            bad.append(f"{fn}: {got} (want {want})")
        assert want not in EASY and want != "mixed", f"{fn} label {want} is not a hard class"
    assert not bad, "sustained-hard files regressed:\n" + "\n".join(bad)


def test_content_and_index_agree_on_reclassified_files(classifications, index_rows, labels):
    """The two caches must not desync on the corrected files (match_zwo reads the
    index; /api/workouts reads the classification JSON)."""
    bad = []
    for fn, want in labels.items():
        row = index_rows.get(fn)
        if row is None:
            bad.append(f"{fn}: missing from index")
        elif row.get("ContentClass") != want:
            bad.append(f"{fn}: index ContentClass={row.get('ContentClass')} != {want}")
    assert not bad, "cache/index disagree on reclassified files:\n" + "\n".join(bad)


def test_no_mixed_in_cache(classifications):
    """v104 forbids ``mixed`` (test_classifier_v104::test_no_mixed_class); the
    correction must never introduce it."""
    offenders = [f for f, e in classifications.items() if e.get("primary") == "mixed"]
    assert offenders == [], f"{len(offenders)} entries are primary=mixed"


def test_correction_excludes_recovery_prefix(labels):
    """recovery_*.zwo are test-locked to recovery (a recovery ride with a mild
    opener is legitimate — test_library_consistency::test_recovery_prefix_consistent).
    The correction must not touch them; its label set contains none."""
    offenders = [f for f in labels if f.startswith("recovery_")]
    assert not offenders, f"correction wrongly targets recovery_ files: {offenders}"


def test_strides_example_intact(classifications):
    """The canonical 'Z2 + short strides' example stays endurance_intervals (it is
    genuinely short pops on an aerobic base — the correct half of the fix)."""
    e = classifications.get("endurance_20s129s_6x_60min.zwo", {})
    assert e.get("primary") == "endurance_intervals"
