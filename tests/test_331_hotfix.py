"""3.3.1 hotfix — regression suite for the v3.3.0 field incident.

Incident chain (DIAG_L1/L2, tester log): the frozen v3.3.0 app didn't bundle
scripts/classify_library_content.py → the facts v2 rebuild raised per file →
every file minted {"null": true} → file_admissible fail-closed everywhere →
E_MATCH_ZWO_NO_CANDIDATES storm → recalc rebuilt the whole plan into Z2
placeholders. Plus surface bugs: stale ICU events protected forever after a
manual swap, 422 bodies discarded, and an implausible auto-ingested FTP
(122 on a ~258 rider) scaling every power target.
"""
import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import workout_facts as wf
import training_planner as tp

ROOT = Path(__file__).resolve().parent.parent


# ── A: packaging ──────────────────────────────────────────────────────────

def test_spec_bundles_classifier():
    """The storm's root cause: the frozen app shipped without the classifier
    script. The spec must bundle it — a string test so packaging can't
    silently regress."""
    spec = (ROOT / "packaging" / "domestique.spec").read_text(encoding="utf-8")
    assert "classify_library_content" in spec


# ── A: facts infra-failsafe + null-heal ───────────────────────────────────

def _seed_dir(tmp_path, n=2):
    d = tmp_path / "workouts"
    d.mkdir()
    src = sorted((ROOT / "src" / "workouts").glob("z2_*.zwo"))[:n]
    assert len(src) == n, "repo z2 fixtures missing"
    for p in src:
        (d / p.name).write_bytes(p.read_bytes())
    return d


def test_classifier_missing_aborts_rebuild_no_nulls(tmp_path, monkeypatch):
    """THE storm regression test: classifier import failure must abort the
    rebuild (old cache preserved on disk, zero null rows minted) instead of
    poisoning every row."""
    d = _seed_dir(tmp_path)
    wf.reset_cache()
    # Prime a healthy cache, then force a version mismatch so ensure_facts
    # wants a full rebuild.
    wf.ensure_facts(d)
    cache_file = d / wf.FACTS_FILENAME
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    real_rows = dict(payload["facts"])
    assert real_rows and not any(r.get("null") for r in real_rows.values())
    payload["version"] = payload["version"] - 1  # stale schema on disk
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    wf.reset_cache()
    monkeypatch.setattr(
        wf, "_clc",
        lambda: (_ for _ in ()).throw(FileNotFoundError("classifier missing")))

    facts = wf.ensure_facts(d)

    # Served facts: the stale rows (degraded), never an all-null map.
    assert facts, "abort path must serve stale rows, not an empty map"
    assert not any(r.get("null") for r in facts.values())
    # Disk: untouched old-version cache — poison was NOT persisted.
    on_disk = json.loads(cache_file.read_text(encoding="utf-8"))
    assert on_disk["version"] == payload["version"]
    assert not any(r.get("null") for r in on_disk["facts"].values())
    wf.reset_cache()


def test_null_heal_recomputes_poisoned_rows(tmp_path):
    """A cache poisoned by the storm (null rows with matching sha1) heals on
    the first boot with a working classifier."""
    d = _seed_dir(tmp_path)
    wf.reset_cache()
    wf.ensure_facts(d)
    cache_file = d / wf.FACTS_FILENAME
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    poisoned = {
        name: {"sha1": row.get("sha1"), "null": True}
        for name, row in payload["facts"].items()
    }
    payload["facts"] = poisoned
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    wf.reset_cache()

    facts = wf.ensure_facts(d)

    assert facts and not any(r.get("null") for r in facts.values()), \
        "null rows with matching sha1 must recompute (heal), not stick"
    wf.reset_cache()


# ── A: recalc circuit breaker ─────────────────────────────────────────────

def test_pool_collapse_reason_unit():
    lib = [{"File": f"f{i}.zwo"} for i in range(200)]
    empty = {"all_pool": [], "hit": [], "endurance": [], "by_class": {}}
    assert tp._pool_collapse_reason(empty, lib)
    tiny = {"all_pool": lib[:2], "hit": lib[:1], "endurance": lib[:1],
            "by_class": {}}
    assert tp._pool_collapse_reason(tiny, lib)  # 1% < 2% floor
    healthy = {"all_pool": lib[:150], "hit": lib[:70], "endurance": lib[:70],
               "by_class": {}}
    assert tp._pool_collapse_reason(healthy, lib) == ""
    # Trivial libraries (unit fixtures) are exempt — never falsely trip.
    assert tp._pool_collapse_reason(empty, lib[:50]) == ""


# ── A: FTP-test placement ────────────────────────────────────────────────

@pytest.mark.parametrize("seed", [0, 7])
def test_ftp_test_previous_day_is_easy(seed):
    """The tester got the mid-cycle test the day after a 99-TSS O/U session.
    The injector must pick a slot whose previous calendar day is rest/easy."""
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import PLANNER_PIN_ARGS
    goal = tp.Goal(goal_type="ftp", plan_weeks=12, hours_per_week=8.0,
                   max_weekday_hours=2.0, max_weekend_hours=3.0,
                   available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[0])
    _ph, weeks = tp.generate_plan(goal, seed_salt=seed, **PLANNER_PIN_ARGS)
    by_day = {s.day: s.session_type for w in weeks for s in w.sessions}
    easy = {"rest", "z2", "recovery"}
    tests = [s for w in weeks for s in w.sessions
             if s.session_type == "ftp_test"]
    assert tests, "mid-cycle FTP test missing from an ftp-goal plan"
    for s in tests:
        prev = by_day.get(s.day - timedelta(days=1))
        assert prev is None or prev in easy, (
            f"seed{seed}: FTP test on {s.day} follows a hard day ({prev})")


def test_ftp_test_counts_as_hard():
    assert "ftp_test" in tp._HIT_SESSION_TYPES


# ── B: swap-sync sweep exemption ──────────────────────────────────────────

def _push_pm():
    pm = MagicMock()
    pm.prefs = {}
    pm._athlete = {"target_mode": "power"}
    pm.lthr_is_set = True
    pm.max_hr = 190
    pm.lthr = 170
    return pm


def test_user_swapped_fileless_not_broken_protected(monkeypatch):
    """A session made fileless by a DELIBERATE user swap must not be
    broken-protected — the stale calendar event must sweep. A genuinely
    broken (unmatched) session stays protected."""
    import icu_calendar_push as icp
    today = date.today()
    day = (today + timedelta(days=2)).isoformat()
    plan = {"weeks": [{"sessions": [
        {"day": day, "session_type": "z2", "zwo_file": "",
         "duration_min": 60, "tss_estimate": 45, "user_swapped": True},
        {"day": day, "session_type": "threshold", "zwo_file": "",
         "duration_min": 60, "tss_estimate": 90},
    ]}]}
    events, skipped, broken = icp._desired_events(
        _push_pm(), plan, today, 14, "pidX")
    ids = {f"domestique:pidX:{day}:0", f"domestique:pidX:{day}:1"}
    assert f"domestique:pidX:{day}:0" not in broken, \
        "user-swapped fileless session must NOT be sweep-protected"
    assert f"domestique:pidX:{day}:1" in broken, \
        "genuinely-unmatched session must stay protected"
    assert not events
    assert ids  # shape sanity


# ── B: 422 observability ─────────────────────────────────────────────────

def test_http_error_detail_logs_body(caplog):
    import icu_calendar_push as icp
    with caplog.at_level("WARNING"):
        detail = icp._http_error_detail(
            "bulk_upsert", 422, b'{"error":"start_date_local invalid"}')
    assert "start_date_local invalid" in detail
    assert any("icu_push_http_error" in r.message and "422" in r.message
               for r in caplog.records)


# ── B: FTP auto-ingest plausibility guard ─────────────────────────────────

def test_ftp_auto_ingest_guard():
    import app as appmod
    ok = appmod._ftp_auto_ingest_ok
    assert not ok(122, 258)   # the incident: implausible drop → rejected
    assert ok(240, 258)       # normal drift → accepted
    assert not ok(95, 0)      # sub-100W absolute → rejected
    assert ok(150, 0)         # no current value, plausible → accepted
    assert not ok("junk", 258)
    assert ok(160, 258)       # >60% boundary region sanity (62%)
