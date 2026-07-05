"""P5 — canaries: both live incidents reproduced-and-rejected, plus the
owner's 4×13 30/15 file admitted exactly where its content belongs.

Incident 1 (sampler): neuromuscular_15s25s_30x_90min.zwo (content-IF 0.967)
was served into a sprint slot — the auto-sampler pool build had no IF gate.
Incident 2 (match_zwo): neuromuscular_4x10s_70min.zwo carried a sweet_spot
label (sub-dose → zone-dominance fallback) and legally matched an SS slot,
landing ~740W spikes on an SS day.

Read-only against the committed caches (library rows come from the on-disk
index; no self-heal writes).
"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import training_planner as tp  # noqa: E402
import workout_facts as wf  # noqa: E402

WK = ROOT / "workouts"
INCIDENT_SAMPLER = "neuromuscular_15s25s_30x_90min.zwo"
INCIDENT_MATCH = "neuromuscular_4x10s_70min.zwo"
NEW_4X13 = "vo2_short_30s15s_4x13_64min.zwo"

pytestmark = pytest.mark.skipif(
    not (WK / wf.FACTS_FILENAME).exists(), reason="facts cache absent")


@pytest.fixture(scope="module")
def rows():
    """Rows from the RUNTIME-healed library, not the committed index file.

    The committed workouts/.library_index.json is a runtime-healed DERIVED
    cache (the never-commit-index-drift rule keeps it byte-identical to HEAD);
    workouts/.content_classification.json is the committed source of truth.
    So we heal in-process exactly as the app does at boot — load_workout_library
    reparses new/changed *.zwo and self-heals index + facts — then read those
    rows. The committed index is restored afterward so the run stays hermetic.
    """
    backup = (WK / ".library_index.json").read_bytes()
    wf.reset_cache()
    tp._WORKOUT_LIB_CACHE.clear()
    tp._WORKOUT_LIB_FAST_VALIDATOR.clear()
    tp._CONTENT_CLASSIFICATION_CACHE = None
    healed = tp.load_workout_library()
    yield healed
    if (WK / ".library_index.json").read_bytes() != backup:
        (WK / ".library_index.json").write_bytes(backup)
    wf.reset_cache()


def _row(rows, fname):
    r = next((r for r in rows if r["File"] == fname), None)
    assert r is not None, f"canary file missing from index: {fname}"
    return r


def _sess(stype, dur):
    return tp.PlannedSession(
        day=date(2026, 7, 7), day_name="Tue", session_type=stype,
        duration_min=dur, tss_estimate=60, description="t")


# ── incident 1: sampler sprint slot ──────────────────────────────────────────

def test_canary_incident1_sampler_reproduced_and_rejected(rows):
    r = _row(rows, INCIDENT_SAMPLER)
    # REPRODUCTION: every pre-existing pool-build gate admitted this row
    # (no ftp tag, Score >= 5, HIT content class) — only the new facts/IF
    # contract keeps it out.
    assert "ftp_test" not in {(t or "").lower() for t in (r.get("Tags") or [])}
    assert (r.get("Score") or 0) >= 5
    assert tp._content_class_for_row(r) in tp._HIT_CONTENT_CLASSES
    assert float(r.get("IF") or 0) > tp._SPRINT_SLOT_IF_CEILING  # the pathology
    # REJECTION: predicate + every sampler pool
    assert tp.file_admissible("sprint", r) is False
    pools = tp._build_pool_indexes(rows)
    for bucket in ("hit", "endurance", "endurance_strict", "all_pool"):
        assert all(w["File"] != INCIDENT_SAMPLER for w in pools[bucket]), bucket
    for cc, rws in pools["by_class"].items():
        assert all(w["File"] != INCIDENT_SAMPLER for w in rws), cc


def test_canary_incident1_real_sampler_over_seeds(rows):
    pools = tp._build_pool_indexes(rows)
    phase = type("P", (), {"name": "build1"})()
    budget = tp.get_budget_for_phase("build1")
    served = set()
    for salt in range(6):
        sessions = tp.sample_week_workouts(
            phase=phase, budget=budget, library=rows, used_names=set(),
            week_num=1 + salt, seed_salt=salt, week_start=date(2026, 7, 6),
            available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[6],
            daily_max_hours=None, max_weekday_hours=1.5,
            max_weekend_hours=2.0, pool_index=pools)
        for s in sessions:
            if s is not None and s.zwo_file:
                served.add(s.zwo_file)
    assert INCIDENT_SAMPLER not in served


# ── incident 2: match_zwo sweet-spot slot ────────────────────────────────────

def test_canary_incident2_match_zwo_reproduced_and_rejected(rows):
    r = _row(rows, INCIDENT_MATCH)
    # The D2 repair relabeled it neuromuscular (ledgered); the facts gate
    # rejects it from SS slots EVEN IF the old sweet_spot label came back.
    assert (r.get("ContentClass") or "") == "neuromuscular"
    mislabeled = dict(r, ContentClass="sweet_spot")  # resurrect the old lie
    assert tp.file_admissible("sweetspot", mislabeled) is False  # t200 > 0
    assert tp.file_admissible("sweetspot", r) is False
    # end-to-end: SS slots across durations/weeks/seeds never return it
    for dur in (50, 60, 75):
        for wk in (1, 3, 6):
            for salt in (0, 1):
                s = tp.match_zwo(_sess("sweetspot", dur), rows, week_num=wk,
                                 day_idx=1, seed_salt=salt)
                assert s.zwo_file != INCIDENT_MATCH, (dur, wk, salt)


def test_canary_incident2_rejected_even_when_only_candidate(rows):
    """With a library REDUCED to the incident file, an SS slot must raise
    NoCandidateWorkoutError rather than serve it (facts gate, not luck)."""
    lib = [_row(rows, INCIDENT_MATCH)]
    with pytest.raises(tp.NoCandidateWorkoutError):
        tp.match_zwo(_sess("sweetspot", 60), lib, week_num=1, day_idx=1,
                     raise_on_empty=True)
    mislabeled = [dict(lib[0], ContentClass="sweet_spot")]
    with pytest.raises(tp.NoCandidateWorkoutError):
        tp.match_zwo(_sess("sweetspot", 60), mislabeled, week_num=1, day_idx=1,
                     raise_on_empty=True)


# ── the owner's 4×13 30/15 file: admitted to vo2, rejected elsewhere ─────────

def test_canary_4x13_admitted_to_vo2_rejected_from_sprint_ss_z2(rows):
    r = _row(rows, NEW_4X13)
    assert tp.file_admissible("vo2max", r) is True    # hi_s >= 240s
    assert tp.file_admissible("sprint", r) is False   # IF 0.92 + no sprint reps
    lib = [r]
    got = tp.match_zwo(_sess("vo2max", 64), lib, week_num=1, day_idx=1,
                       raise_on_empty=True)
    assert got.zwo_file == NEW_4X13                   # vo2 slots: served
    for slot, dur in (("sprint", 45), ("sweetspot", 60), ("z2", 60)):
        with pytest.raises(tp.NoCandidateWorkoutError):
            tp.match_zwo(_sess(slot, dur), lib, week_num=1, day_idx=1,
                         raise_on_empty=True)
