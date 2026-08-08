"""P1/P6 — L1 facts layer: determinism, incremental build, semantics,
fail-closed null rows, full-library index parity (A1, FreeRide-aware) and the
new-file inline-facts self-heal path (A5).

Hermetic: synthetic tests run in tmp libraries; full-library tests read the
committed caches READ-ONLY (no load_workout_library call → no self-heal
writes).
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import training_planner as tp  # noqa: E402
import workout_facts as wf  # noqa: E402

WK = ROOT / "workouts"


# ── synthetic ZWO helpers ────────────────────────────────────────────────────

def _zwo(name: str, body: str, tags: str = "") -> str:
    return (
        "<?xml version='1.0' encoding='utf-8'?>\n<workout_file>\n"
        "    <author>t</author>\n"
        f"    <name>{name}</name>\n"
        "    <description>t</description>\n"
        "    <sportType>bike</sportType>\n"
        f"{tags}"
        "    <workout>\n" + body + "    </workout>\n</workout_file>"
    )


STEADY = '        <SteadyState Duration="{d}" Power="{p}" />\n'
INTER = '        <IntervalsT Repeat="{r}" OnDuration="{on}" OffDuration="{off}" OnPower="{onp}" OffPower="{offp}" />\n'
WARMUP = '        <Warmup Duration="{d}" PowerLow="{lo}" PowerHigh="{hi}" />\n'
FREE = '        <FreeRide Duration="{d}" />\n'


def _mklib(tmp: Path, files: dict[str, str]) -> Path:
    lib = tmp / "workouts"
    lib.mkdir(exist_ok=True)
    for fn, content in files.items():
        (lib / fn).write_text(content, encoding="utf-8")
    return lib


def _basic_files() -> dict[str, str]:
    return {
        "z2_endurance_45min.zwo": _zwo("Z2", WARMUP.format(d=300, lo=0.4, hi=0.6)
                                       + STEADY.format(d=2400, p=0.68)),
        "sweetspot_2x15min_45min.zwo": _zwo("SS", WARMUP.format(d=300, lo=0.4, hi=0.7)
                                            + STEADY.format(d=900, p=0.9) * 2),
        "sprints_4x10s_40min.zwo": _zwo("SP", WARMUP.format(d=300, lo=0.4, hi=0.6)
                                        + INTER.format(r=4, on=10, off=170, onp=2.5, offp=0.5)
                                        + STEADY.format(d=1200, p=0.6)),
        "vo2_5x3min-3min_108pct_45min.zwo": _zwo("V", WARMUP.format(d=300, lo=0.4, hi=0.7)
                                     + INTER.format(r=5, on=180, off=180, onp=1.12, offp=0.5)),
    }


@pytest.fixture()
def tmplib(tmp_path):
    lib = _mklib(tmp_path, _basic_files())
    wf.reset_cache()
    yield lib
    wf.reset_cache()


# ── determinism + incremental (GW1) ─────────────────────────────────────────

def test_facts_two_builds_byte_identical(tmplib):
    wf.ensure_facts(tmplib)
    b1 = (tmplib / wf.FACTS_FILENAME).read_bytes()
    (tmplib / wf.FACTS_FILENAME).unlink()
    wf.reset_cache()
    wf.ensure_facts(tmplib)
    b2 = (tmplib / wf.FACTS_FILENAME).read_bytes()
    assert b1 == b2


def test_facts_incremental_and_prune(tmplib):
    facts = wf.ensure_facts(tmplib)
    sha_before = {fn: r["sha1"] for fn, r in facts.items()}
    # edit ONE file, delete another
    (tmplib / "z2_endurance_45min.zwo").write_text(
        _zwo("Z2", STEADY.format(d=2400, p=0.70)), encoding="utf-8")
    (tmplib / "vo2_5x3min-3min_108pct_45min.zwo").unlink()
    facts2 = wf.ensure_facts(tmplib)
    assert "vo2_5x3min-3min_108pct_45min.zwo" not in facts2  # pruned
    assert facts2["z2_endurance_45min.zwo"]["sha1"] != sha_before["z2_endurance_45min.zwo"]
    for fn in ("sweetspot_2x15min_45min.zwo", "sprints_4x10s_40min.zwo"):
        assert facts2[fn]["sha1"] == sha_before[fn]  # untouched rows stable


def test_facts_column_semantics(tmp_path):
    lib = _mklib(tmp_path, {
        "mix.zwo": _zwo("M", WARMUP.format(d=300, lo=0.4, hi=0.6)
                        + INTER.format(r=4, on=10, off=50, onp=2.5, offp=0.5)
                        + STEADY.format(d=60, p=1.6)
                        + FREE.format(d=300)),
    })
    wf.reset_cache()
    f = wf.ensure_facts(lib)["mix.zwo"]
    assert f["dur_s"] == 300 + 4 * 60 + 60 + 300      # A1: INCL FreeRide
    assert f["fr_s"] == 300
    assert f["t240"] == 40 and f["l240"] == 10        # 4×10s @2.5
    assert f["t200"] == 40 and f["l200"] == 10
    assert f["t150"] == 100 and f["l150"] == 60       # + 60s @1.6
    assert f["sprints"] == 4                          # classifier sprint reps
    assert f["n130_45"] == 1                          # the 60s @1.6 run
    assert f["hi_s"] == 100                           # z7 seconds (2.5 + 1.6)
    wf.reset_cache()


def test_unparseable_file_null_row_fail_closed(tmp_path, monkeypatch):
    lib = _mklib(tmp_path, _basic_files())
    (lib / "broken_60min.zwo").write_text("<workout_file><workout>", encoding="utf-8")
    wf.reset_cache()
    facts = wf.ensure_facts(lib)
    assert facts["broken_60min.zwo"].get("null") is True
    monkeypatch.setattr(tp, "WORKOUT_DIR", lib)
    row = {"File": "broken_60min.zwo", "IF": 0.6, "Score": 5}
    for slot in ("z2", "recovery", "sweetspot", "threshold", "vo2max", "sprint"):
        assert tp.file_admissible(slot, row) is False  # A5 fail-closed
    # missing facts + missing file → also inadmissible
    assert tp.file_admissible("z2", {"File": "ghost.zwo", "IF": 0.6}) is False
    # ungated slot types are not touched by the facts gate
    assert tp.file_admissible("ftp_test", {"File": "broken_60min.zwo"}) is True
    wf.reset_cache()


# ── P6: new-file inline facts via the index self-heal path (A5) ─────────────

def test_new_file_gets_facts_inline_via_library_heal(tmp_path, monkeypatch):
    lib = _mklib(tmp_path, _basic_files())
    wf.reset_cache()
    monkeypatch.setattr(tp, "WORKOUT_DIR", lib)
    tp._WORKOUT_LIB_CACHE.clear()
    tp._WORKOUT_LIB_FAST_VALIDATOR.clear()
    monkeypatch.setattr(tp, "_CONTENT_CLASSIFICATION_CACHE", {})
    rows = tp.load_workout_library()
    assert len(rows) == 4
    facts = wf.load_facts(lib)
    assert set(facts) == set(_basic_files())
    # drop a NEW file in — the next library load must self-heal its facts row
    (lib / "threshold_2x20min_60min.zwo").write_text(
        _zwo("T", WARMUP.format(d=300, lo=0.4, hi=0.7)
             + STEADY.format(d=1200, p=1.0) * 2), encoding="utf-8")
    tp._WORKOUT_LIB_CACHE.clear()
    tp._WORKOUT_LIB_FAST_VALIDATOR.clear()
    rows2 = tp.load_workout_library()
    assert len(rows2) == 5
    f = wf.load_facts(lib).get("threshold_2x20min_60min.zwo")
    assert f and not f.get("null")
    assert f["t200"] == 0 and f["hi_s"] == 0
    # …and the new file is admissible where its content belongs
    row = next(r for r in rows2 if r["File"] == "threshold_2x20min_60min.zwo")
    assert tp.file_admissible("threshold", row) is True
    # a supra-carrying sibling stays out of the recovery contract (t130 > 0)
    sp = next(r for r in rows2 if r["File"] == "sprints_4x10s_40min.zwo")
    assert tp.file_admissible("recovery", sp) is False
    assert tp.file_admissible("z2", sp) is False        # t200 > 0
    wf.reset_cache()


# ── full-library parity (P1, A1) — against the RUNTIME-healed state ──────────

@pytest.mark.skipif(not (WK / wf.FACTS_FILENAME).exists(), reason="facts cache absent")
def test_full_library_facts_index_parity():
    # The committed .library_index.json + .workout_facts.json are runtime-healed
    # DERIVED caches (never-commit-index-drift keeps the index byte-identical to
    # HEAD); .content_classification.json is the committed source of truth. Heal
    # in-process exactly as the app does at boot, then compare the healed facts
    # against the healed index. Restore the committed index afterward (hermetic).
    _idx_backup = (WK / ".library_index.json").read_bytes()
    wf.reset_cache()
    tp._WORKOUT_LIB_CACHE.clear()
    tp._WORKOUT_LIB_FAST_VALIDATOR.clear()
    tp._CONTENT_CLASSIFICATION_CACHE = None
    healed_rows = tp.load_workout_library()
    try:
        facts = wf.load_facts(WK)
        idx = {r["File"]: r for r in healed_rows}
        _run_parity(facts, idx)
    finally:
        if (WK / ".library_index.json").read_bytes() != _idx_backup:
            (WK / ".library_index.json").write_bytes(_idx_backup)
        wf.reset_cache()


def _run_parity(facts, idx):
    joined = 0
    dur_bad, if_bad, tss_bad, null_rows = [], [], [], []
    for fn, f in facts.items():
        if f.get("null"):
            null_rows.append(fn)
            continue
        r = idx.get(fn)
        if r is None:
            continue  # facts may lead the index when files land mid-session
        joined += 1
        if abs(f["dur_s"] / 60.0 - (r.get("Duration(min)") or 0)) > 0.6:
            dur_bad.append(fn)
        if f["fr_s"] == 0:  # A1: IF/TSS parity only on FreeRide-free files
            if abs(f["if"] - (r.get("IF") or 0)) > 0.02:
                if_bad.append(fn)
            t = r.get("TSS") or 0
            if t and abs(f["tss"] - t) > max(2.0, 0.03 * t):
                tss_bad.append(fn)
    assert joined > 4000
    assert dur_bad == [], f"duration parity broke on {len(dur_bad)}: {dur_bad[:5]}"
    assert if_bad == [], f"IF parity broke on {len(if_bad)}: {if_bad[:5]}"
    assert tss_bad == [], f"TSS parity broke on {len(tss_bad)}: {tss_bad[:5]}"
