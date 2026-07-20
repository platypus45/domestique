"""v3.5.0 — library TSS/IF switched from RMS power to Coggan NP.

Pins the metric change end to end:
  * the NP primitive against the research-report goldens (synthetic files
    from /tmp/TSS_RESEARCH_REPORT.md §2.2, incl. the DELIBERATE sub-30s
    blindness — NP/AP == 1.0 for reps at or under the 30s window is inherent
    to the Coggan recipe; do NOT "fix" it, the work-above-CP axis is the
    designed answer),
  * commensurability with ride_storage.compute_power_tss (the entire point:
    planned and ridden TSS must be in the same units — README documents NP
    and the ride side always used it; the library side was RMS),
  * the shipped caches (index + facts) for the calibrated flagship file,
  * the schema versions, so a future formula edit must bump them again.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import ride_storage
import training_planner as tp
import workout_facts as wf

REPO = Path(__file__).resolve().parent.parent
FLAGSHIP = "anaerobic_4x3min_67min.zwo"


def _np(samples):
    return tp._np_fraction_from_samples(samples)


# ── NP primitive: research-report goldens ────────────────────────────────────

def test_golden_300s_duty_cycle_np_premium():
    # §2.2: 5min@160% / 5min@40%, 1:1 duty → NP/AP = 1.334.
    w = ([1.6] * 300 + [0.4] * 300) * 4
    ratio = _np(w) / (sum(w) / len(w))
    assert ratio == pytest.approx(1.334, abs=0.002)


@pytest.mark.parametrize("on,off", [(15, 15), (5, 5)])
def test_sub30s_blindness_is_pinned_behaviour(on, off):
    # Periods dividing the 30s window collapse to NP == AP exactly. Inherent
    # to Coggan NP (§2.2 "commensurability artifact") — pinned so nobody
    # patches the window and silently breaks comparability.
    w = ([1.6] * on + [0.4] * off) * (2400 // (on + off))
    assert _np(w) == pytest.approx(sum(w) / len(w), rel=1e-9)


def test_np_undefined_below_one_window():
    assert _np([1.0] * 29) == 0.0
    assert _np([]) == 0.0


def test_left_truncated_window_constant_series_identity():
    # A constant series must give NP == the constant under full-window
    # semantics (zero-padding or an expanding mean would drag it down).
    assert _np([0.87] * 300) == pytest.approx(0.87, rel=1e-12)


# ── Commensurability with the ride side ──────────────────────────────────────

def test_library_np_matches_ride_storage_on_same_series():
    ftp = 258
    frac = ([1.1] * 180 + [0.55] * 120) * 8
    lib_if = _np(frac)
    lib_tss = len(frac) / 3600 * lib_if ** 2 * 100
    ride_tss = ride_storage.compute_power_tss([f * ftp for f in frac], ftp)
    # ride side rounds to 1 dp — allow exactly that.
    assert ride_tss == pytest.approx(lib_tss, abs=0.06)


# ── Shipped caches: flagship calibration ─────────────────────────────────────

def _index_rows():
    idx = json.loads((REPO / "workouts" / ".library_index.json").read_text())
    return idx, {r["File"]: r for r in idx["rows"]}


def test_index_schema_v3_and_flagship_row():
    idx, rows = _index_rows()
    assert idx["schema_version"] == 3
    fl = rows[FLAGSHIP]
    assert fl["IF"] == pytest.approx(0.819, abs=0.002)
    assert fl["TSS"] == pytest.approx(75.0, abs=0.5)


def test_facts_v3_flagship_matches_index():
    data = json.loads((REPO / "workouts" / ".workout_facts.json").read_text())
    assert data["version"] == 3
    row = data["facts"][FLAGSHIP]
    assert row["if"] == pytest.approx(0.819, abs=0.005)
    assert row["tss"] == pytest.approx(75.0, abs=1.0)


def test_schema_versions_pinned():
    # A future TSS/IF formula change MUST bump these (stale installed caches
    # validate on .zwo mtimes, which a formula edit does not touch).
    assert tp._INDEX_SCHEMA_VERSION == 3
    assert wf._SCHEMA_VERSION == 3
