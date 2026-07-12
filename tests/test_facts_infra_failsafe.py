"""3.3.1 hotfix — regression suite for the v3.3.0 no-candidates storm
(DIAG_L1, evidence-locked diagnosis).

Incident chain: the frozen v3.3.0 app shipped WITHOUT
scripts/classify_library_content.py → every facts recompute raised
FileNotFoundError inside compute_facts_row's per-file try → 4,255
{"null": true} rows were minted and PERSISTED as a valid v2 cache →
file_admissible failed closed for every gated slot type → match_zwo /
sampler pools empty → the weekly auto-recalc "successfully" rebuilt every
future week into unmatched Z2 placeholders. Fix set covered here:

  F1  domestique.spec bundles the classifier script (string canary).
  F2  infra failure (classifier module unimportable) ABORTS the facts
      rebuild: old cache retained on disk (even old-version), zero null
      rows minted, stale rows served in-memory → match_zwo still matches.
      THE STORM REGRESSION TEST — this is the test that would have caught
      the incident. Per-FILE parse failures still null only that file.
  F3  a poisoned all-null v2 cache HEALS in place on the next boot with a
      working classifier (nulls were sticky at the sha1-skip before).
  F4  recalculate_plan / regenerate_from_today circuit breaker: an
      effectively-empty pool index over a non-trivial library aborts the
      mass rebuild and keeps the existing plan (with a UI-surfaceable
      reason) instead of Z2-flattening it.
  F5  _inject_mid_cycle_ftp_tests places the test on a slot whose previous
      calendar day is rest/easy (cross week boundary), and ftp_test counts
      as a HIT type (consumes a weekly hard slot; 48h passes see it).
"""
from __future__ import annotations

import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

import training_planner as tp
import workout_facts as wf
from conftest import PLANNER_PIN_ANCHOR, PLANNER_PIN_ARGS, FrozenPlannerDate

ROOT = Path(__file__).resolve().parent.parent
WK = ROOT / "workouts"

# Real library files, hand-picked so their COMMITTED facts pass their slot
# gates (threshold: t240==0 & t200==0; z2: n130_45==0 & t200==0; vo2max:
# hi_s>=240; recovery: t130==0). Real files keep the storm test honest about
# score floors / classification / category fallbacks in match_zwo.
STORM_FILES = [
    "threshold_10s120s_12x_53min.zwo",
    "threshold_10x1min_56min.zwo",
    "threshold_10x2min_60min_renamed_v46_1.zwo",
    "endurance_10s240s_9x_62min.zwo",
    "endurance_1min_6x_60min_renamed_v46_1.zwo",
    "endurance_20s129s_6x_60min.zwo",
    "vo2max_10x1min_49min.zwo",
    "vo2max_10x2min_42min.zwo",
    "vo2max_10x2min_60min.zwo",
    "recovery_10x30s_60min.zwo",
    "recovery_2x0min_52min.zwo",
    "recovery_3x0min_67min.zwo",
]
GATED_TYPES = ("threshold", "vo2max", "z2", "recovery")


@pytest.fixture()
def storm_lib(tmp_path, monkeypatch):
    """Tmp library of REAL files + the real classification map, with the
    planner pointed at it and every relevant cache cold. Hermetic: the
    committed workouts/ dir is only ever read."""
    lib = tmp_path / "workouts"
    lib.mkdir()
    for fn in STORM_FILES:
        shutil.copy2(WK / fn, lib / fn)
    shutil.copy2(WK / ".content_classification.json",
                 lib / ".content_classification.json")
    wf.reset_cache()
    monkeypatch.setattr(tp, "WORKOUT_DIR", lib)
    monkeypatch.setattr(tp, "_CONTENT_CLASSIFICATION_CACHE", None)
    tp._WORKOUT_LIB_CACHE.clear()
    tp._WORKOUT_LIB_FAST_VALIDATOR.clear()
    yield lib
    wf.reset_cache()
    tp._WORKOUT_LIB_CACHE.clear()
    tp._WORKOUT_LIB_FAST_VALIDATOR.clear()


def _v1_downgrade(lib: Path) -> None:
    """Rewrite the tmp lib's facts cache as schema v1 (strip the v2-only
    t101/l101 columns) — the exact on-disk state of a v3.2.x install."""
    payload = json.loads((lib / wf.FACTS_FILENAME).read_text())
    v1 = {fn: {k: v for k, v in r.items() if k not in ("t101", "l101")}
          for fn, r in payload["facts"].items()}
    (lib / wf.FACTS_FILENAME).write_text(
        json.dumps({"version": 1, "facts": v1}))


def _match_types(library) -> dict[str, str]:
    """zwo_file matched per gated slot type (deterministic: fixed args)."""
    out = {}
    for st in GATED_TYPES:
        s = tp.PlannedSession(day=date(2026, 1, 6), day_name="Tue",
                              session_type=st, duration_min=60,
                              tss_estimate=60, description="t")
        m = tp.match_zwo(s, library, raise_on_empty=False)
        out[st] = getattr(m, "zwo_file", "") or ""
    return out


def _null_count(rows: dict) -> int:
    return sum(1 for r in rows.values() if r.get("null"))


# ── F2: THE STORM REGRESSION TEST ────────────────────────────────────────────

def test_storm_classifier_missing_plus_v1_cache_never_nulls(storm_lib, monkeypatch):
    """Frozen-build simulation: classifier unimportable + v1 facts on disk.
    The rebuild must ABORT (old cache retained, zero nulls minted) and
    match_zwo must keep finding candidates — under v3.3.0 this exact state
    nulled the whole library and persisted the poison."""
    lib = storm_lib
    # Healthy boot: builds the v2 facts + the CONTROL match set.
    rows = tp.load_workout_library()
    assert len(rows) == len(STORM_FILES)
    healthy = _match_types(rows)
    assert any(healthy.values()), "fixture broken: nothing matches healthy"

    # Now the incident state: v1 cache, cold caches, index miss forced, and
    # the classifier import raising exactly what the frozen app raised.
    _v1_downgrade(lib)
    (lib / ".library_index.json").unlink(missing_ok=True)
    wf.reset_cache()
    tp._WORKOUT_LIB_CACHE.clear()
    tp._WORKOUT_LIB_FAST_VALIDATOR.clear()

    def _boom():
        raise FileNotFoundError(
            "scripts/classify_library_content.py missing (frozen bundle)")
    monkeypatch.setattr(wf, "_clc", _boom)

    rows2 = tp.load_workout_library()  # ensure_facts runs (and aborts) inside
    assert len(rows2) == len(STORM_FILES)

    # 1. Rebuild ABORTED: the old v1 cache is retained byte-for-byte intact.
    on_disk = json.loads((lib / wf.FACTS_FILENAME).read_text())
    assert on_disk["version"] == 1, "old-version cache must be left in place"
    # 2. ZERO null rows minted — on disk AND in memory.
    assert _null_count(on_disk["facts"]) == 0
    assert _null_count(wf.load_facts(lib)) == 0
    # 3. match_zwo parity: every type that matched healthy still matches
    #    under the classifier outage (stale v1 rows drive the gates).
    broken = _match_types(rows2)
    for st, hf in healthy.items():
        if hf:
            assert broken[st], (
                f"{st}: matched with a healthy classifier but NOT under the "
                f"classifier outage — the v3.3.0 storm is back")


def test_infra_failure_get_facts_never_persists_nulls(storm_lib, monkeypatch):
    """Per-file inline heal under classifier outage: a file with no cache row
    reports missing (fail-closed for THAT file only) and nothing is written."""
    lib = storm_lib
    tp.load_workout_library()
    _v1_downgrade(lib)
    wf.reset_cache()
    monkeypatch.setattr(wf, "_clc", lambda: (_ for _ in ()).throw(
        FileNotFoundError("no classifier")))
    before = (lib / wf.FACTS_FILENAME).read_bytes()
    # Known file → served from the stale v1 fallback.
    assert wf.get_facts(lib, STORM_FILES[0]) is not None
    # Brand-new file (no v1 row) → None, and NOTHING persisted.
    shutil.copy2(WK / "ftp_test_coggan_20min.zwo", lib / "ftp_test_coggan_20min.zwo")
    assert wf.get_facts(lib, "ftp_test_coggan_20min.zwo") is None
    assert (lib / wf.FACTS_FILENAME).read_bytes() == before


def test_per_file_parse_failure_still_nulls_only_that_file(storm_lib):
    """F2 must NOT weaken A5: with a HEALTHY classifier, an unparseable file
    still gets a null row — and only that file."""
    lib = storm_lib
    (lib / "broken_60min.zwo").write_text("<workout_file><workout>",
                                          encoding="utf-8")
    facts = wf.ensure_facts(lib)
    assert facts["broken_60min.zwo"].get("null") is True
    assert _null_count(facts) == 1


# ── F3: poisoned-cache heal ──────────────────────────────────────────────────

def test_poisoned_all_null_v2_cache_heals_on_next_boot(storm_lib):
    """The incident's persisted end-state: a VALID v2 cache whose every row
    is {"sha1": <true sha1>, "null": true}. Pre-fix these were sticky forever
    (sha1 match → skip). With a working classifier, ensure_facts must
    recompute them all and matching must work again."""
    lib = storm_lib
    rows = tp.load_workout_library()          # healthy v2 facts
    payload = json.loads((lib / wf.FACTS_FILENAME).read_text())
    poison = {fn: {"sha1": r["sha1"], "null": True}
              for fn, r in payload["facts"].items()}
    (lib / wf.FACTS_FILENAME).write_text(
        json.dumps({"version": 2, "facts": poison},
                   sort_keys=True, separators=(",", ":")))
    wf.reset_cache()

    healed = wf.ensure_facts(lib)
    assert _null_count(healed) == 0, "null rows must heal, not stick"
    on_disk = json.loads((lib / wf.FACTS_FILENAME).read_text())
    assert on_disk["version"] == 2
    assert _null_count(on_disk["facts"]) == 0
    # …and the gates open again end-to-end.
    matched = _match_types(rows)
    assert any(matched.values())
    thr = next(r for r in rows if r["File"] == STORM_FILES[0])
    assert tp.file_admissible("threshold", thr) is True


# ── F1: packaging canary ─────────────────────────────────────────────────────

def test_spec_bundles_classifier_script():
    """workout_facts._clc() loads the classifier by FILE PATH, invisible to
    PyInstaller's import scan — only an explicit datas entry ships it. v3.3.0
    shipped without it; this canary stops the packaging from regressing
    silently."""
    spec = (ROOT / "domestique.spec").read_text(encoding="utf-8")
    assert '("scripts/classify_library_content.py", "scripts")' in spec, (
        "domestique.spec no longer bundles scripts/classify_library_content.py"
        " — the frozen app cannot compute workout facts without it (v3.3.0"
        " no-candidates storm)")


# ── F4: pool-collapse circuit breaker ────────────────────────────────────────

_EMPTY_POOLS = {"hit": [], "endurance": [], "endurance_strict": [],
                "by_class": {}, "all_pool": []}


def _fake_library(n=200):
    return [{"File": f"fake_{i}.zwo", "Name": f"F{i}", "Score": 5, "Tags": []}
            for i in range(n)]


def _mk_weeks(n=4):
    """Hand-built plan: this week + (n-1) future weeks, alternating hard/easy."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    weeks = []
    for i in range(n):
        ws = monday + timedelta(weeks=i)
        sessions = [
            tp.PlannedSession(day=ws + timedelta(days=d), day_name="",
                              session_type=("threshold" if d in (1, 4) else "z2"),
                              duration_min=60, tss_estimate=60,
                              description="x", zwo_file=f"wk{i}d{d}.zwo")
            for d in range(7)
        ]
        weeks.append(tp.PlannedWeek(week_num=i + 1, start=ws,
                                    end=ws + timedelta(days=6), phase="build1",
                                    tss_target=400, is_stepback=False,
                                    sessions=sessions))
    return weeks


def _breaker_goal():
    # Granfondo (target CTL ≥85) at CTL 45 → deviation >8% → rebuild path
    # (same shape test_recalc_preserves_state proves reaches "recalculated").
    return tp.Goal(goal_type="event",
                   target_date=date.today() + timedelta(days=56),
                   event_name="GF", event_km=120, event_climb_m=1500,
                   event_type="granfondo", hours_per_week=10.0,
                   max_weekday_hours=2.0, max_weekend_hours=4.0,
                   rest_days=[0])


@pytest.fixture(autouse=True)
def _reset_distribution():
    yield
    tp.set_active_distribution("polarized", None)


def test_recalc_circuit_breaker_keeps_plan_on_pool_collapse(monkeypatch):
    goal, weeks = _breaker_goal(), _mk_weeks()
    before = [[(s.day, s.session_type, s.zwo_file) for s in w.sessions]
              for w in weeks]
    monkeypatch.setattr(tp, "load_workout_library", lambda *a, **k: _fake_library())
    monkeypatch.setattr(tp, "_build_pool_indexes", lambda lib: dict(_EMPTY_POOLS))
    phases, all_weeks, info = tp.recalculate_plan(goal, weeks, current_ctl=45)
    assert info["action"] == "no_change", info
    assert info["reason"] == "pool_collapse"
    assert info.get("detail"), "UI-surfaceable detail missing"
    assert phases == []
    assert all_weeks is weeks, "must return the EXISTING plan object"
    after = [[(s.day, s.session_type, s.zwo_file) for s in w.sessions]
             for w in weeks]
    assert after == before, "breaker must not mutate a single session"


def test_recalc_healthy_pools_still_rebuild(monkeypatch):
    """Control: the breaker must not fire on the real library — the rebuild
    proceeds exactly as before the hotfix."""
    goal, weeks = _breaker_goal(), _mk_weeks()
    phases, all_weeks, info = tp.recalculate_plan(goal, weeks, current_ctl=45)
    assert info["action"] == "recalculated", info
    assert info.get("reason") != "pool_collapse"
    assert info.get("weeks_regenerated", 0) >= 1


def test_regen_circuit_breaker_raises_before_any_rebuild(monkeypatch):
    goal, weeks = _breaker_goal(), _mk_weeks()
    monkeypatch.setattr(tp, "load_workout_library", lambda *a, **k: _fake_library())
    monkeypatch.setattr(tp, "_build_pool_indexes", lambda lib: dict(_EMPTY_POOLS))
    with pytest.raises(ValueError, match="temporarily unavailable"):
        tp.regenerate_from_today(goal, weeks, current_ctl=45)


def test_breaker_never_trips_on_small_or_healthy_libraries():
    """Unit truth-table for _pool_collapse_reason: tiny synthetic libraries
    (slot-gate tests) and healthy ratios never trip; the storm signature
    (22/4255, endurance=0) does."""
    row = {"File": "x.zwo"}
    # tiny library, even fully inadmissible → never trips
    assert tp._pool_collapse_reason(dict(_EMPTY_POOLS), [row] * 40) == ""
    # healthy ratio (73%) → no trip
    healthy = dict(_EMPTY_POOLS, all_pool=[row] * 150, hit=[row] * 60,
                   endurance=[row] * 90)
    assert tp._pool_collapse_reason(healthy, [row] * 200) == ""
    # the storm signature → trips
    storm = dict(_EMPTY_POOLS, all_pool=[row] * 22, hit=[row] * 22)
    assert tp._pool_collapse_reason(storm, [row] * 4255) != ""
    assert tp._pool_collapse_reason(dict(_EMPTY_POOLS), [row] * 4255) != ""


# ── F5: FTP-test placement ───────────────────────────────────────────────────

_EASY = {"rest", "z2", "long_z2", "recovery"}


@pytest.fixture()
def pinned_planner(monkeypatch):
    """Function-scoped pin (frozen today + no live metrics fetch) so nothing
    leaks into the other tests in this module (the conftest fixture is
    module-scoped by design; here tests with REAL today coexist)."""
    monkeypatch.setattr(tp, "date", FrozenPlannerDate)
    monkeypatch.setattr(tp, "get_today_metrics", lambda: {})
    yield


def test_ftp_test_prev_day_easy_and_no_hard_pileup(pinned_planner):
    """Across ≥3 seeds of a pinned 20-week plan: every injected FTP test sits
    on a day whose PREVIOUS calendar day is rest/easy (cross week boundary),
    and the test is never part of a ≥4-day consecutive-hard run. Pre-fix the
    injector took the first hard slot blindly (the day after build1's final
    Sunday) and the cap/48h passes were blind to ftp_test."""
    for seed in (0, 1, 4242):
        goal = tp.Goal(goal_type="event",
                       target_date=PLANNER_PIN_ANCHOR + timedelta(weeks=20),
                       event_type="granfondo", event_km=120,
                       event_climb_m=1500, hours_per_week=10.0,
                       max_weekday_hours=2.0, max_weekend_hours=4.0,
                       rest_days=[0], plan_weeks=20)
        _phases, weeks = tp.generate_plan(goal, seed_salt=seed,
                                          **PLANNER_PIN_ARGS)
        tests = [(w, s) for w in weeks for s in w.sessions
                 if s.session_type == "ftp_test"]
        assert tests, f"seed {seed}: 20-week cycle must schedule FTP tests"
        day_map = {s.day: s for w in weeks for s in w.sessions}
        hard_days = {s.day for w in weeks for s in w.sessions
                     if tp._session_is_hit(s)}
        for w, s in tests:
            prev = day_map.get(s.day - timedelta(days=1))
            assert prev is None or prev.session_type in _EASY, (
                f"seed {seed}: FTP test on {s.day} follows a "
                f"{prev.session_type} day — must follow rest/easy")
            lo = hi = s.day
            while lo - timedelta(days=1) in hard_days:
                lo -= timedelta(days=1)
            while hi + timedelta(days=1) in hard_days:
                hi += timedelta(days=1)
            streak = (hi - lo).days + 1
            assert streak <= 3, (
                f"seed {seed}: FTP test on {s.day} sits in a {streak}-day "
                f"consecutive-hard run")
            # the test CONSUMES a weekly hard slot (cap sees it)
            cap = tp.get_budget_for_phase(w.phase).hit_count_max
            assert tp._week_hit_count(w) <= cap, (
                f"seed {seed}: test week exceeds its weekly HIT cap")


def test_ftp_test_is_hit_and_cap_never_demotes_it():
    """ftp_test ∈ _HIT_SESSION_TYPES (counts toward the cap / 48h passes) but
    the weekly-cap pass demotes the hard volume AROUND it, never the test."""
    assert "ftp_test" in tp._HIT_SESSION_TYPES
    monday = date(2026, 1, 5)
    mk = lambda d, t: tp.PlannedSession(
        day=monday + timedelta(days=d), day_name="", session_type=t,
        duration_min=60, tss_estimate=70, description="")
    # build1 cap is 3; 3 vo2max + the test = 4 HIT → one vo2max must go.
    wk = tp.PlannedWeek(
        week_num=5, start=monday, end=monday + timedelta(days=6),
        phase="build1", tss_target=500, is_stepback=False,
        sessions=[mk(0, "ftp_test"), mk(1, "vo2max"), mk(3, "vo2max"),
                  mk(5, "vo2max"), mk(6, "z2")])
    library = tp.load_workout_library()
    tp._enforce_weekly_hit_cap([wk], library)
    types = [s.session_type for s in wk.sessions]
    assert types.count("ftp_test") == 1, "the cap pass demoted the FTP test"
    cap = tp.get_budget_for_phase("build1").hit_count_max
    assert tp._week_hit_count(wk) <= cap
