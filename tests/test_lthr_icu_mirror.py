"""v2.5.0 W3: LTHR + max_hr mirror from the newest synced ICU activity.

Unit-level coverage of db._refresh_hr_from_activities(): the activities
table is an in-memory SQLite stub, ProfileManager is stubbed (pattern:
tests/test_hr_mode_api.py _StubPM) — tests never touch the real
athlete.json or ~/.domestique DB.
"""
import json
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db as db_mod  # noqa: E402
import profile_manager as pm_mod  # noqa: E402

TODAY = date.today().isoformat()


class _StubPM:
    """Minimal ProfileManager stand-in. save_athlete mimics the real float
    cast + [100,220] lthr validator; _set_max_hr mimics the real source
    priority (manual > icu > computed > age_tanaka) and [140,220] clamp."""

    def __init__(self, athlete):
        self._athlete = dict(athlete)
        self.save_calls = []
        self.set_max_hr_calls = []

    def save_athlete(self, data):
        self.save_calls.append(dict(data))
        d = dict(data)
        if d.get("lthr") is not None:
            v = float(d["lthr"])
            if not (100 <= v <= 220):
                raise ValueError(f"lthr={v} out of range [100,220]")
            d["lthr"] = v
        self._athlete.update(d)

    def _set_max_hr(self, value, source):
        self.set_max_hr_calls.append((value, source))
        _PRIO = {"manual": 3, "icu": 2, "computed": 1, "age_tanaka": 0}
        v = int(float(value))
        if not (140 <= v <= 220):
            return False
        cur = self._athlete.get("max_hr_source")
        if cur in _PRIO and _PRIO[source] < _PRIO[cur]:
            return False
        self._athlete["max_hr"] = v
        self._athlete["max_hr_source"] = source
        return True


def _run(monkeypatch, athlete, acts):
    """Run the mirror against a stub profile + activity rows.

    acts: list of (id, date, payload_dict) — payload becomes raw_json.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE activities (id TEXT PRIMARY KEY, date TEXT, raw_json TEXT)"
    )
    for aid, dt, payload in acts:
        conn.execute(
            "INSERT INTO activities (id, date, raw_json) VALUES (?, ?, ?)",
            (aid, dt, json.dumps(payload)),
        )
    stub = _StubPM(athlete)
    monkeypatch.setattr(db_mod, "get_db", lambda: conn)
    monkeypatch.setattr(pm_mod.ProfileManager, "get", classmethod(lambda cls: stub))
    db_mod._refresh_hr_from_activities()
    return stub


def _assert_not_swallowed(caplog):
    """The mirror wraps everything in try/except — a stub bug would silently
    log 'failed' and fake a no-op. Every test checks it didn't."""
    assert not [r for r in caplog.records
                if "refresh_hr_from_activities failed" in r.getMessage()]


# ── mirrors when source != manual ────────────────────────────────────────────

def test_mirrors_lthr_and_max_hr_into_empty_profile(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="db")
    stub = _run(monkeypatch, {}, [
        ("i1", "2026-07-01", {"lthr": 177, "athlete_max_hr": 195}),
    ])
    assert stub._athlete["lthr"] == 177
    assert stub._athlete["lthr_source"] == "icu"
    assert stub._athlete["lthr_source_date"] == TODAY
    assert stub.set_max_hr_calls == [(195, "icu")]
    assert stub._athlete["max_hr"] == 195
    assert [r for r in caplog.records if r.levelno == logging.INFO
            and "hr_mirror_from_activity" in r.getMessage()]
    _assert_not_swallowed(caplog)


def test_mirrors_over_non_manual_source(monkeypatch, caplog):
    stub = _run(monkeypatch,
                {"lthr": 165, "lthr_source": "icu_estimate",
                 "lthr_source_date": "2026-06-14"},
                [("i1", "2026-07-01", {"lthr": 177, "athlete_max_hr": 195})])
    assert stub._athlete["lthr"] == 177
    assert stub._athlete["lthr_source"] == "icu"
    assert stub._athlete["lthr_source_date"] == TODAY
    _assert_not_swallowed(caplog)


# ── blocked when manual ──────────────────────────────────────────────────────

def test_manual_lthr_never_clobbered_but_max_hr_still_mirrors(monkeypatch, caplog):
    stub = _run(monkeypatch,
                {"lthr": 165, "lthr_source": "manual",
                 "lthr_source_date": "2026-05-01"},
                [("i1", "2026-07-01", {"lthr": 177, "athlete_max_hr": 195})])
    assert stub._athlete["lthr"] == 165
    assert stub._athlete["lthr_source"] == "manual"
    assert stub._athlete["lthr_source_date"] == "2026-05-01"
    assert stub.save_calls == []          # no save_athlete write at all
    assert stub._athlete["max_hr"] == 195  # max path independent of lthr block
    _assert_not_swallowed(caplog)


def test_manual_max_hr_blocks_max_mirror_but_lthr_still_mirrors(monkeypatch, caplog):
    stub = _run(monkeypatch,
                {"max_hr": 190, "max_hr_source": "manual"},
                [("i1", "2026-07-01", {"lthr": 177, "athlete_max_hr": 195})])
    assert stub._athlete["max_hr"] == 190
    assert stub.set_max_hr_calls == []    # pre-checked, not even attempted
    assert stub._athlete["lthr"] == 177
    _assert_not_swallowed(caplog)


# ── range guards ─────────────────────────────────────────────────────────────

def test_implausible_values_skipped_falls_back_to_next_newest(monkeypatch, caplog):
    stub = _run(monkeypatch, {}, [
        ("i2", "2026-07-01", {"lthr": 250, "athlete_max_hr": 139}),  # both bad
        ("i1", "2026-06-20", {"lthr": 175, "athlete_max_hr": 192}),
    ])
    assert stub._athlete["lthr"] == 175
    assert stub._athlete["max_hr"] == 192
    _assert_not_swallowed(caplog)


def test_all_implausible_is_silent_noop(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="db")
    stub = _run(monkeypatch, {"lthr": 170}, [
        ("i1", "2026-07-01", {"lthr": 99, "athlete_max_hr": 230}),
    ])
    assert stub._athlete["lthr"] == 170
    assert stub.save_calls == []
    assert stub.set_max_hr_calls == []
    assert not caplog.records  # silent: no INFO, no WARNING
    _assert_not_swallowed(caplog)


def test_non_numeric_payload_values_ignored(monkeypatch, caplog):
    stub = _run(monkeypatch, {}, [
        ("i1", "2026-07-01", {"lthr": "high", "athlete_max_hr": None}),
    ])
    assert stub.save_calls == []
    assert stub.set_max_hr_calls == []
    _assert_not_swallowed(caplog)


# ── max_hr > lthr invariant ──────────────────────────────────────────────────

def test_violating_pair_skips_both_writes_with_warning(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="db")
    stub = _run(monkeypatch, {}, [
        ("i1", "2026-07-01", {"lthr": 180, "athlete_max_hr": 175}),
    ])
    assert stub.save_calls == []
    assert stub.set_max_hr_calls == []
    warns = [r for r in caplog.records if r.levelno == logging.WARNING
             and "hr mirror skipped" in r.getMessage()]
    assert len(warns) == 1
    _assert_not_swallowed(caplog)


def test_mirrored_max_hr_must_exceed_existing_manual_lthr(monkeypatch, caplog):
    # lthr blocked (manual, 170); payload max 165 would land BELOW it → skip.
    stub = _run(monkeypatch,
                {"lthr": 170, "lthr_source": "manual", "max_hr": 190},
                [("i1", "2026-07-01", {"lthr": 175, "athlete_max_hr": 165})])
    assert stub._athlete["max_hr"] == 190
    assert stub.set_max_hr_calls == []
    assert [r for r in caplog.records if r.levelno == logging.WARNING
            and "hr mirror skipped" in r.getMessage()]
    _assert_not_swallowed(caplog)


# ── no-op when unchanged ─────────────────────────────────────────────────────

def test_unchanged_values_write_nothing_and_log_nothing(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="db")
    # 177.0 float mimics save_athlete's cast — numeric equality must hold.
    stub = _run(monkeypatch,
                {"lthr": 177.0, "lthr_source": "icu",
                 "lthr_source_date": "2026-06-14",
                 "max_hr": 195, "max_hr_source": "icu"},
                [("i1", "2026-07-01", {"lthr": 177, "athlete_max_hr": 195})])
    assert stub.save_calls == []
    assert stub.set_max_hr_calls == []
    assert stub._athlete["lthr_source_date"] == "2026-06-14"  # no daily churn
    assert not caplog.records
    _assert_not_swallowed(caplog)


def test_no_activities_is_silent_noop(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="db")
    stub = _run(monkeypatch, {"lthr": 170}, [])
    assert stub.save_calls == []
    assert stub.set_max_hr_calls == []
    assert not caplog.records
    _assert_not_swallowed(caplog)


# ── newest-activity selection ────────────────────────────────────────────────

def test_newest_activity_wins_regardless_of_insert_order(monkeypatch, caplog):
    stub = _run(monkeypatch, {}, [
        ("i9", "2026-07-01", {"lthr": 177, "athlete_max_hr": 195}),
        ("i1", "2026-06-01", {"lthr": 170, "athlete_max_hr": 188}),  # older, inserted last
    ])
    assert stub._athlete["lthr"] == 177
    assert stub._athlete["max_hr"] == 195
    _assert_not_swallowed(caplog)


def test_fields_picked_independently_from_newest_plausible_carrier(monkeypatch, caplog):
    # Newest ride has only a plausible lthr; max_hr comes from the next one.
    stub = _run(monkeypatch, {}, [
        ("i2", "2026-07-01", {"lthr": 177}),
        ("i1", "2026-06-20", {"lthr": 176, "athlete_max_hr": 195}),
    ])
    assert stub._athlete["lthr"] == 177
    assert stub._athlete["max_hr"] == 195
    _assert_not_swallowed(caplog)
