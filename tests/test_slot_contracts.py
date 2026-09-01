"""P3/P4 — D3 slot-contract invariant + pool floors, full library.

The core watertightness property: for EVERY (slot_type, file) pair the
planner's gates admit, the file's content facts satisfy the slot's locked
contract. Plus P4 pool-coverage floors so the gates can never strangle a
(type × duration) band into NoCandidateWorkoutError storms, and pool purity
for the auto-sampler (every pooled row admissible for the type it would
serve).

Read-only against the committed caches — no planner self-heal writes.
"""
import json
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

# v3.2.2 (#15 R1): consume match_zwo's REAL maps (hoisted to module
# constants) — the mirrored copy silently under-covered whenever the live
# maps gained a class (e.g. endurance_intervals on z2).
CAT = tp._TYPE_TO_CONTENT_CLASS
FB = tp._TYPE_TO_FALLBACK_CLASSES
EASYC = {"recovery": 25.0, "z2": 40.0, "long_z2": 40.0}
BUCKETS = (30, 45, 60, 75, 90)
# P4 floor table (locked): 15 per (type × bucket) cell, except sprint@75: 30
# and sprint@90: 8 (alert tier — a 90-min sprint slot is incident-1's
# pathology; the planner-side TYPE_CEILING cap keeps real sprint sessions
# far below it, pinned in test_sprint_duration_cap below).
FLOORS = {s: dict.fromkeys(BUCKETS, 15) for s in CAT}
FLOORS["sprint"][75] = 30
FLOORS["sprint"][90] = 8


# Heal the library in-process (as the app does at boot) so the invariant runs
# against the RUNTIME view — the committed index is a byte-identical-to-HEAD
# derived cache (never-commit-index-drift) that still lists the 3 renamed-away
# files and lacks the new ones; healing reconciles ghosts + new rows so every
# admitted row resolves to facts. content_classification.json is the committed
# source of truth. Same pattern as test_canaries_watertight.rows.
_HEALED: dict = {}


@pytest.fixture(scope="module", autouse=True)
def _heal_library():
    backup = (WK / ".library_index.json").read_bytes()
    wf.reset_cache()
    tp._WORKOUT_LIB_CACHE.clear()
    tp._WORKOUT_LIB_FAST_VALIDATOR.clear()
    tp._CONTENT_CLASSIFICATION_CACHE = None
    _HEALED["rows"] = tp.load_workout_library()
    _HEALED["facts"] = json.loads((WK / wf.FACTS_FILENAME).read_text())["facts"]
    yield
    if (WK / ".library_index.json").read_bytes() != backup:
        (WK / ".library_index.json").write_bytes(backup)
    wf.reset_cache()


def _rows():
    return _HEALED["rows"]


def _facts():
    return _HEALED["facts"]


def _bulk_pool(rows, slot, tgt):
    """Mirror of match_zwo's pre-facts bulk gates (class/score/dur/IF/z345/SF)."""
    out = []
    for r in rows:
        cc = (r.get("ContentClass") or "").strip().lower()
        if not cc:
            continue
        tags = {(t or "").lower() for t in (r.get("Tags") or [])}
        if "ftp_test" in tags:
            continue
        if cc in ("endurance", "recovery"):
            if (r.get("Score") or 0) < 1 or (r.get("Duration(min)") or 0) < 20:
                continue
        elif (r.get("Score") or 0) < 3:
            continue
        if slot == "sprint" and float(r.get("IF") or 0) > tp._SPRINT_SLOT_IF_CEILING:
            continue
        if slot in ("z2", "long_z2", "recovery") and float(r.get("IF") or 0) > tp._EASY_SLOT_IF_CEILING:
            continue
        if abs((r.get("Duration(min)") or 0) - tgt) > max(15.0, tgt * 0.25):
            continue
        if cc != CAT[slot] and cc not in FB[slot]:
            continue
        ec = EASYC.get(slot)
        if ec is not None:
            z345 = sum(float(r.get(f"Z{i}%") or 0) for i in (3, 4, 5, 6))
            if z345 >= ec:
                continue
            sf = r.get("SecondaryFlags") or {}
            if any(sf.get(k) for k in ("has_threshold_work", "has_vo2_work",
                                       "has_sprints", "pattern_over_under")):
                continue
        out.append(r)
    return out


def _contract_holds(slot, f, row):
    """Re-state the locked D3 table independently of file_admissible."""
    if f is None or f.get("null"):
        return False
    if slot == "sprint":
        return (float(row.get("IF") or 0) <= tp._SPRINT_SLOT_IF_CEILING
                and f["t150"] >= 60 and f["sprints"] >= 4)
    if slot in ("sweetspot", "tempo"):
        # R4/R5 (2026-07-07): + sustained supra-FTP ceiling (facts schema v2).
        return (f["t200"] == 0 and f["l150"] < 45 and f["t150"] <= 30
                and f["l101"] < 300)
    if slot in ("threshold", "overunder"):
        return f["t240"] == 0 and f["t200"] == 0
    if slot == "vo2max":
        return f["hi_s"] >= 240
    if slot in ("z2", "long_z2"):
        return f["n130_45"] == 0 and f["t200"] == 0
    if slot == "recovery":
        return f["t130"] == 0
    return True


def test_full_library_slot_contract_invariant():
    """∀ (slot, file) admitted by bulk gates + file_admissible: contract holds,
    and file_admissible agrees with the independently restated table."""
    rows, facts = _rows(), _facts()
    checked = 0
    for slot in CAT:
        for tgt in BUCKETS:
            for r in _bulk_pool(rows, slot, tgt):
                adm = tp.file_admissible(slot, r)
                expected = _contract_holds(slot, facts.get(r["File"]), r)
                assert adm == expected, (slot, tgt, r["File"], adm, expected)
                if adm:
                    checked += 1
    assert checked > 10000  # the invariant actually swept real pools


def test_pool_floors_hold():
    rows = _rows()
    thin = []
    for slot, per_bucket in FLOORS.items():
        for tgt, floor in per_bucket.items():
            n = sum(1 for r in _bulk_pool(rows, slot, tgt)
                    if tp.file_admissible(slot, r))
            if n < floor:
                thin.append((slot, tgt, n, floor))
    assert thin == [], f"pool floors violated: {thin}"


def test_sprint_duration_cap_planner_side():
    """The sprint@90 alert tier is acceptable BECAUSE the planner hard-caps
    sprint/neuromuscular session length far below 90 (TYPE_CEILING), so a
    90-min sprint slot cannot survive to the rider."""
    assert tp.TYPE_CEILING["sprint"] <= 75
    assert tp.TYPE_CEILING["neuromuscular"] <= 75


def test_sampler_pools_contain_only_admissible_rows():
    """Call site 3/3: every row in every sampler pool satisfies the contract
    for the session type it would serve (_session_type_from_row)."""
    rows = _rows()
    pools = tp._build_pool_indexes(rows)
    seen = 0
    for bucket in ("hit", "endurance", "endurance_strict", "all_pool"):
        for r in pools[bucket]:
            assert tp.file_admissible(tp._session_type_from_row(r), r), \
                (bucket, r["File"])
            seen += 1
    for cc, rws in pools["by_class"].items():
        for r in rws:
            assert tp.file_admissible(tp._session_type_from_row(r), r), \
                ("by_class", cc, r["File"])
            seen += 1
    assert seen > 5000


def test_emergency_fallback_never_serves_ftp_class(tmp_path, monkeypatch):
    """4th call site pins (coordinator delta): a class=ftp_test row — real
    test or mislabeled workout — must never surface on a normal day via the
    class-blind emergency fallback; a TAGGED real test never enters pools."""
    lib = tmp_path / "workouts"
    lib.mkdir()
    monkeypatch.setattr(tp, "WORKOUT_DIR", lib)
    wf.reset_cache()
    mis = {"Name": "FTP Test 2x10", "File": "ftp_test_2x10min-4min_100pct_42min.zwo",
           "Duration(min)": 42.0, "TSS": 60.0, "IF": 0.8, "Score": 6,
           "Protocol": "FTP Test", "Notes": "", "Z1%": 20.0, "Z2%": 20.0,
           "Z3%": 10.0, "Z4%": 50.0, "Z5%": 0.0, "Z6%": 0.0, "Tags": [],
           "ContentClass": "ftp_test", "ContentConfidence": 0.9,
           "SecondaryFlags": {}}
    tagged = dict(mis, File="ftp_test_coggan_3x1min-1min_95pct_59min.zwo", Tags=["ftp_test"])
    pools = tp._build_pool_indexes([mis, tagged])
    assert pools["hit"] == [] and pools["endurance"] == []
    assert tagged not in pools["all_pool"]          # tag skip at pool build
    # v3.2.2 (#14 grill amendment 5): the UNTAGGED ftp_test-classed row is now
    # excluded at pool admission too (was only caught downstream by the
    # emergency fallback's class filter) — no pool carries it at all.
    assert mis not in pools["all_pool"]
    assert mis in pools["by_class"].get("ftp_test", [])  # want_test paths keep it
    budget = tp.get_budget_for_phase("base")
    phase = type("P", (), {"name": "base"})()
    sessions = tp.sample_week_workouts(
        phase=phase, budget=budget, library=[mis, tagged], used_names=set(),
        week_num=1, seed_salt=0, week_start=__import__("datetime").date(2026, 7, 6),
        available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[],
        daily_max_hours=None, max_weekday_hours=1.0, max_weekend_hours=1.5,
        pool_index=pools)
    for s in sessions:
        if s is None or s.session_type == "rest":
            continue
        assert (s.zwo_file or "") not in {mis["File"], tagged["File"]}, \
            f"ftp_test-classed file served on a normal slot: {s.zwo_file}"
    wf.reset_cache()
