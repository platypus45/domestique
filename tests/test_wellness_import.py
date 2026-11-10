"""3.14.0 F1 — the morning check-in from intervals.icu wellness.

The 30-minute sync already fetches intervals.icu's wellness records; their
subjective answers (soreness, fatigue, stress, sleep quality, mood,
motivation, injury) sat unused in raw_json. The import fills the day's
morning check-in (daily_log) from them, stored exactly as the form stores
the button with the same label, so every reader sees the number a tap in
Domestique would have given.

intervals.icu scales — not in its OpenAPI schema; documented by its developer
on the public forum (read 2026-11-09/10, no API call made):
  * soreness / fatigue / stress: 0 None, 1 Low, 2 Avg, 3 High, 4 Extreme
    (code list, forum.intervals.icu/t/best-way-to-integrate-apple-watch-
    apple-health-data-into-intervals-icu/5776 post 12, 2022-06-15; "1-4 scale
    where 1 is good", forum.intervals.icu/t/api-access-to-intervals-icu/609
    post 34, 2021-01-02; UI labels Low/Avg/High/Extreme,
    forum.intervals.icu/t/wellness-data-ordering-of-mood-stress-soreness-
    scores/19416, 2022-09-10)
  * sleepQuality: 1 Great, 2 Good, 3 Average, 4 Poor — often filled from a
    watch's sleep score (forum.intervals.icu/t/garmin-sleep-score-and-quality-
    integration/8778, 2022-03-12; .../suunto-wellness-data-now-integrated/
    91204, 2025-02-19)
  * mood 1 Great .. 4 Grumpy, motivation 1 Extreme .. 4 Low, injury 1 None,
    2 Niggle, 3 Poor, 4 Injured (code list above; forum.intervals.icu/t/
    graphing-wellness-data/9870; .../welness-injury-field/5843)
OWNER CHECK BEFORE RELEASE (plan_wellness §2, grill M1): in the intervals.icu
UI, confirm whether the Soreness/Fatigue/Stress pickers offer "None" below
"Low", and that Extreme / Grumpy / Poor chart as 4 with invert off. If any
reads 1, the direction flipped upstream and test_t1 must change first.
Result: <pending — record it here>.

Grill fixes pinned here: C1/C2 (outcomes with the new bands, RB1 included so
the import can never ship on the old bands), H2 (a watch's sleep is not an
answer), M2 (a cleared answer reverts; nothing left deletes the import's
row; a missing record keeps everything), M3 (an injury counts as sore legs,
and the verdict always carries a plain line about it), M7 (rider-level
outcomes, not only form equivalence), L1 (the race with a form save), L3
(one warning per pass on an unmigrated database).
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import sys
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as app_module                       # noqa: E402
import db                                      # noqa: E402
import readiness_composite as rc               # noqa: E402
import training_planner as tp                  # noqa: E402
from test_injury_gates_integration import _IntegrationBase  # noqa: E402

GREAT, GOOD, NORMAL, TIRED, WORST = (db._FORM_STORE[k] for k in (1, 2, 3, 4, 5))
DAY = date(2026, 11, 10)
DAY_ISO = DAY.isoformat()
COLS = db.HOOPER_COLS
DASH = ROOT / "src" / "templates" / "dashboard.html"


@pytest.fixture()
def fresh_db(tmp_path):
    original = db.DB_PATH
    db.set_db_path(tmp_path / "wellness_import.db")
    db.init_db()
    db._sync_stop.clear()
    app_module.clear_cache()
    try:
        yield tmp_path / "wellness_import.db"
    finally:
        db.set_db_path(original)


@pytest.fixture()
def today(fresh_db, freeze_clock):
    freeze_clock(DAY)
    return DAY_ISO


def _sync(rows):
    with patch.object(db, "fetch_wellness", return_value=rows):
        return db.sync_wellness(days=30)


def _rec(day=DAY_ISO, **fields):
    return {"id": day, "ctl": 40.0, "atl": 38.0, **fields}


def _row(day=DAY_ISO):
    r = db.get_db().execute("SELECT * FROM daily_log WHERE date = ?", (day,)).fetchone()
    return dict(r) if r else None


def _values(row):
    return tuple(row[c] for c in ("sleep_quality", "fatigue", "stress", "soreness"))


# ── T1/T2: the scale, pinned ─────────────────────────────────────────────────

SCALE = (
    [(k, code, db._FORM_STORE[code + 1]) for k in ("soreness", "fatigue", "stress")
     for code in range(5)]
    + [("sleepQuality", code, db._FORM_STORE[code]) for code in range(1, 5)])
COLUMN = {"soreness": "soreness", "fatigue": "fatigue", "stress": "stress",
          "sleepQuality": "sleep_quality"}


@pytest.mark.parametrize("field,code,stored", SCALE,
                         ids=[f"{f}={c}" for f, c, _ in SCALE])
def test_t1_every_intervals_icu_code_maps_to_the_same_labelled_button(field, code, stored):
    rec = {field: code}
    if field == "sleepQuality":
        rec["fatigue"] = 2          # sleep alone is not a check-in (watches fill it)
    got = db.icu_check_in(rec)
    assert got[COLUMN[field]] == stored
    # explicit pins, so a silent change of the table cannot pass by itself
    expected = {0: 1, 1: 3, 2: 4, 3: 6, 4: 7} if field != "sleepQuality" else {1: 1, 2: 3, 3: 4, 4: 6}
    assert stored == expected[code]


def test_t2_every_imported_value_is_a_form_value_and_reads_back_as_its_label():
    """A linear 1+2(v-1) map turns "Avg" into 3, which the form shows as
    "Good": the meaning changes. The label map keeps "Avg" -> "Normal"."""
    form_values = set(db._FORM_STORE.values())
    for field, code, stored in SCALE:
        assert stored in form_values
        level = code + 1 if field != "sleepQuality" else code
        assert round(stored * 5 / 7) == level       # the form's "Change" pre-select


# ── T3/T4/T10: what counts, what is stored ───────────────────────────────────

def test_t3_sleep_or_mood_alone_is_not_a_check_in(today):
    _sync([_rec(sleepQuality=4), _rec(day="2026-11-09", mood=1, motivation=4)])
    assert db.get_db().execute("SELECT COUNT(*) FROM daily_log").fetchone()[0] == 0
    assert db.get_db().execute("SELECT COUNT(*) FROM wellness").fetchone()[0] == 2


def test_t4_one_answer_stores_the_others_as_untouched_normal(today):
    _sync([_rec(soreness=3)])
    r = _row()
    assert _values(r) == (NORMAL, NORMAL, NORMAL, TIRED)
    assert r["mood"] == 4 and r["readiness_to_train"] is None
    assert r["hooper_index"] == 3 * NORMAL + TIRED
    assert r["icu_fields"] == "soreness"


@pytest.mark.parametrize("bad", [5, -1, "3", True, 2.5, {"v": 3}, [3], None])
def test_t10_off_scale_answers_are_not_answers(today, bad):
    _sync([_rec(soreness=bad, fatigue=bad, stress=bad)])
    assert _row() is None
    assert db.get_db().execute("SELECT COUNT(*) FROM wellness").fetchone()[0] == 1


def test_t10_an_integral_float_is_the_code_it_spells(today):
    _sync([_rec(fatigue=3.0)])
    assert _row()["fatigue"] == TIRED


# ── T5: same as the same taps in the form (no history: fixed limits) ─────────

FORM_EQUIVALENT = [
    # (intervals.icu record, the form taps with the same labels)
    (dict(soreness=1, fatigue=1, stress=1, sleepQuality=2),
     dict(sleep_quality=GOOD, fatigue=GOOD, stress=GOOD, soreness=GOOD)),
    (dict(soreness=2, fatigue=2, stress=2, sleepQuality=3),
     dict(sleep_quality=NORMAL, fatigue=NORMAL, stress=NORMAL, soreness=NORMAL)),
    (dict(fatigue=3, sleepQuality=4),
     dict(sleep_quality=TIRED, fatigue=TIRED, stress=NORMAL, soreness=NORMAL)),
    (dict(soreness=3),
     dict(sleep_quality=NORMAL, fatigue=NORMAL, stress=NORMAL, soreness=TIRED)),
]


def _outcomes():
    row = db.get_daily_log_today()
    planned = tp.PlannedSession(day=DAY, day_name="", session_type="threshold",
                                duration_min=60, tss_estimate=80, description="")
    adj, reason = tp.adjust_today_session(planned, {"score": 90, "dfa_cap": {}},
                                          rides_recent=[], daily_log_today=row)
    sev = rc.compute_training_severity("p", DAY_ISO)
    cols = {c: row[c] for c in ("sleep_quality", "fatigue", "stress", "soreness",
                                "mood", "hooper_index")}
    return cols, (adj.session_type, reason), (sev["severity"], sev["reasons"]), \
        app_module._get_soreness_subjective()


@pytest.mark.parametrize("record,taps", FORM_EQUIVALENT)
def test_t5_an_import_acts_exactly_like_the_same_taps(tmp_path, freeze_clock, record, taps):
    freeze_clock(DAY)
    original = db.DB_PATH
    try:
        db.set_db_path(tmp_path / "imported.db")
        db.init_db()
        db._sync_stop.clear()
        _sync([_rec(**record)])
        imported = _outcomes()
        db.set_db_path(tmp_path / "form.db")
        db.init_db()
        r = TestClient(app_module.app).post("/api/daily-log", json={**taps, "mood": 4})
        assert r.status_code == 200, r.text
        assert imported == _outcomes()
    finally:
        db.set_db_path(original)


class TestImportedCheckInThroughTodaySession(_IntegrationBase):
    """End to end: a real sync writes the check-in, the real endpoint reads it."""

    def _import(self, **fields):
        with patch.object(db, "fetch_wellness",
                          return_value=[{"id": date.today().isoformat(), **fields}]):
            db.sync_wellness(days=1)

    def _today_session(self):
        with patch.object(app_module, "_load_all_rides_safe", return_value=[]):
            r = self.client.get("/api/today-session")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_fatigue_high_and_poor_sleep_caps_the_session(self):
        db._sync_stop.clear()
        self._import(fatigue=3, sleepQuality=4)
        out = self._today_session()
        self.assertEqual((out.get("adjusted") or {}).get("session_type"), "z2", out)
        self.assertIn("check-in", out.get("reason") or "")

    # MT6 / C1 / C2 / RB1: rider-level outcomes on the new bands.
    def test_ordinary_imports_never_cap(self):
        db._sync_stop.clear()
        for fields in (dict(fatigue=1), dict(fatigue=3),
                       dict(soreness=2, fatigue=2, stress=2, sleepQuality=3)):
            self._import(**fields)
            out = self._today_session()
            self.assertEqual((out.get("adjusted") or {}).get("session_type"), "vo2max",
                             (fields, out.get("reason")))

    # MT7 / M3: an injury logged as Injured is sore legs: recovery.
    def test_an_injury_alone_forces_recovery(self):
        db._sync_stop.clear()
        self._import(injury=4)
        out = self._today_session()
        self.assertEqual((out.get("adjusted") or {}).get("session_type"), "recovery", out)
        # QX-M6: the card names the injury, not "G5 soreness 6/7".
        self.assertIn("You logged an injury in intervals.icu (Injured)", out.get("reason") or "")


# ── T6/T7: a check-in the rider saved is never written ──────────────────────

def test_t6_a_check_in_from_before_3_14_survives_an_import(today):
    db.upsert_daily_log(DAY_ISO, GOOD, GOOD, GOOD, GOOD, 4, readiness_to_train=7)
    before = _row()
    assert before["icu_fields"] is None
    _sync([_rec(soreness=4, fatigue=4, stress=4, sleepQuality=4)])
    after = _row()
    assert after == before, "the import wrote the rider's own check-in"


def test_t7_a_form_save_makes_an_imported_day_the_riders(today):
    _sync([_rec(fatigue=3)])
    assert _row()["icu_fields"] == "fatigue"
    r = TestClient(app_module.app).post("/api/daily-log", json={
        "sleep_quality": GOOD, "fatigue": GOOD, "stress": GOOD, "soreness": GOOD})
    assert r.status_code == 200
    _sync([_rec(fatigue=4, soreness=4)])
    row = _row()
    assert _values(row) == (GOOD, GOOD, GOOD, GOOD) and row["icu_fields"] is None


# ── T8/T9 (M2): updates on an imported day ───────────────────────────────────

def test_t8_a_changed_answer_replaces_the_imported_one(today):
    _sync([_rec(soreness=1)])
    assert _row()["soreness"] == GOOD
    _sync([_rec(soreness=3)])
    r = _row()
    assert r["soreness"] == TIRED
    assert r["hooper_index"] == 3 * NORMAL + TIRED and r["icu_fields"] == "soreness"


def test_t9_an_answer_cleared_in_intervals_icu_is_cleared_here(today):
    _sync([_rec(fatigue=4, stress=1)])
    assert _row()["fatigue"] == WORST
    _sync([_rec(fatigue=None, stress=1)])      # the rider cleared the mistake
    r = _row()
    assert r["fatigue"] == NORMAL, "a cleared answer kept capping the day"
    assert r["icu_fields"] == "stress"


def test_t9_a_record_with_no_answer_left_removes_the_imported_day(today):
    db.upsert_daily_log("2026-11-08", GOOD, GOOD, GOOD, GOOD, 4)     # the rider's
    _sync([_rec(fatigue=4), _rec(day="2026-11-08", fatigue=4)])
    assert _row() is not None
    _sync([_rec(fatigue=None), _rec(day="2026-11-08")])
    assert _row() is None, "an imported day with nothing left must go"
    assert _row("2026-11-08")["fatigue"] == GOOD, "the rider's day must stay"


def test_t9_a_day_the_sync_did_not_fetch_keeps_everything(today):
    _sync([_rec(day="2026-11-09", fatigue=3), _rec(fatigue=3)])
    _sync([_rec(fatigue=3)])                     # 9 Nov not in this window
    assert _row("2026-11-09")["fatigue"] == TIRED


# ── T11: the import can never cost the wellness batch ───────────────────────

def test_t11_a_failing_import_does_not_roll_back_the_batch(today):
    with patch.object(db, "icu_check_in", side_effect=RuntimeError("boom")):
        _sync([_rec(hrv=61.0, fatigue=3)])
    w = db.get_db().execute("SELECT hrv, ctl FROM wellness WHERE date = ?", (DAY_ISO,)).fetchone()
    assert w is not None and w["hrv"] == 61.0 and w["ctl"] == 40.0
    assert _row() is None


# ── T13: mood is shown, never stored (the composite reads it the other way) ──

def test_t13_intervals_icu_mood_never_reaches_the_mood_column(today):
    _sync([_rec(fatigue=2, mood=1)])
    imported = _row()
    assert imported["mood"] == 4
    db.get_db().execute("DELETE FROM daily_log")
    db.get_db().commit()
    db.upsert_daily_log(DAY_ISO, NORMAL, NORMAL, NORMAL, NORMAL, 4)
    manual = _row()
    feel = lambda row: rc._components_for_date([], {DAY_ISO: row}, {}, DAY_ISO, None)["feel"]
    assert feel(imported) == feel(manual)


# ── H2 (MT2): a watch's sleep is not the rider's answer ──────────────────────

def test_mt2_sleep_quality_with_a_sleep_score_is_the_watchs(today):
    _sync([_rec(fatigue=2, sleepQuality=4, sleepScore=55)])
    r = _row()
    assert r["sleep_quality"] == NORMAL and r["icu_fields"] == "fatigue"
    d = TestClient(app_module.app).get("/api/daily-log").json()
    assert d["sources"]["sleep_quality"] == "default"
    assert d["icu_sleep_from_watch"] is True
    _sync([_rec(fatigue=2, sleepQuality=4)])     # no score: the rider's answer
    r = _row()
    assert r["sleep_quality"] == TIRED and r["icu_fields"] == "fatigue,sleep_quality"
    assert TestClient(app_module.app).get("/api/daily-log").json()["icu_sleep_from_watch"] is False


# ── T14/T15: the API surfaces ────────────────────────────────────────────────

def test_t14_daily_log_says_where_each_answer_came_from(today):
    _sync([_rec(fatigue=3, soreness=2, mood=2, motivation=9, injury=2)])
    d = TestClient(app_module.app).get("/api/daily-log").json()
    assert d["sources"] == {"sleep_quality": "default", "fatigue": "intervals.icu",
                            "soreness": "intervals.icu", "stress": "default"}
    assert d["icu_extras"] == {"mood": "Good", "injury": "Niggle"}   # 9 is off the scale
    assert d["band"]["band"] == "tier_down" and d["band"]["mode"] == "fixed"
    db.upsert_daily_log(DAY_ISO, GOOD, GOOD, GOOD, GOOD, 4)
    d = TestClient(app_module.app).get("/api/daily-log").json()
    assert set(d["sources"].values()) == {"manual"}
    assert d["band"]["band"] == "normal"


_FAKE_TRAINING = {"ctl": 50.0, "atl": 50.0, "tsb": 0.0}
_FAKE_SLEEP = {"ln_rmssd_7d": 3.5, "swc_lower": 3.4, "swc_upper": 3.6,
               "sleep_h": 7.5, "rhr_delta": 0.0}


def _readiness():
    with patch.object(app_module, "cached",
                      side_effect=lambda k, fn: _FAKE_TRAINING if k == "training" else _FAKE_SLEEP), \
         patch.object(app_module, "_local_sleep_metrics", return_value={}), \
         patch.object(app_module, "_merge_training_load", return_value=dict(_FAKE_TRAINING)), \
         patch.object(app_module, "_recent_dfa_and_decoupling",
                      return_value=([], None, None, None)):
        r = TestClient(app_module.app).get("/api/readiness")
    assert r.status_code == 200, r.text
    return r.json()


def test_t15_readiness_flags_an_imported_check_in(today):
    _sync([_rec(fatigue=1)])
    assert _readiness()["checkin_imported"] is True
    db.upsert_daily_log(DAY_ISO, GOOD, GOOD, GOOD, GOOD, 4)
    assert _readiness()["checkin_imported"] is False


def test_l5_readiness_judges_the_day_the_one_clock_says(fresh_db, freeze_clock):
    """The verdict read the machine's date while every other reader read the
    app's clock: under a frozen clock it judged a day with no check-in."""
    other = date(2026, 3, 2)
    freeze_clock(other)
    db.upsert_daily_log(other.isoformat(), TIRED, TIRED, NORMAL, NORMAL, 4)
    out = _readiness()
    assert out["source"] == "hooper" and out["severity"] == "rest", out


# ── MT6 / C1 / C2 / RB1: what the rider sees, on the new bands ───────────────

@pytest.mark.parametrize("fields,severity", [
    (dict(fatigue=1), "normal"),                                     # Low only
    (dict(fatigue=3), "tier_down"),                                  # High only
    (dict(soreness=2, fatigue=2, stress=2, sleepQuality=3), "normal"),  # all Avg
])
def test_mt6_rider_level_outcomes(today, fields, severity):
    none = _readiness()["score_0_100"]
    _sync([_rec(**fields)])
    out = _readiness()
    assert out["source"] == "hooper" and out["severity"] == severity, out
    if severity == "normal":
        assert out["score_0_100"] == none, "an ordinary check-in lowered readiness"


# ── M3 (MT7): an injury ──────────────────────────────────────────────────────

def test_mt7_an_injury_counts_as_sore_legs_and_gets_its_own_line(today):
    _sync([_rec(injury=4)])
    r = _row()
    assert r["soreness"] == TIRED and r["icu_fields"] == "soreness"
    out = _readiness()
    assert out["severity"] != "normal"
    # Wave 4 (QX-M1): the line is worded by today's session; with none
    # planned here, it names the injury without promising a recovery ride
    # (the planned-session case is test_qx_m1_the_injury_line_says_what_happens_today).
    assert "injury" in (out["injury_note"] or "") and "counts as sore legs" in out["injury_note"]


def test_mt7_a_niggle_changes_nothing_but_is_still_named(today):
    _sync([_rec(injury=2)])
    assert _row() is None
    out = _readiness()
    assert out["injury_note"] and "Niggle" in out["injury_note"]


def test_mt7_an_injury_never_lowers_a_worse_soreness_answer(today):
    _sync([_rec(soreness=4, injury=3)])
    assert _row()["soreness"] == WORST


def test_mt7_the_verdict_renders_the_injury_line():
    src = DASH.read_text(encoding="utf-8")
    start = src.index("async function loadReadinessComposite()")
    body = src[start:src.index("\n}\n", start)]
    assert "d.injury_note" in body and "${esc(injuryNote)}" in body


# ── L1 (MT4): a form save racing the sync batch ─────────────────────────────

def test_mt4_a_form_save_during_the_batch_wins(today, monkeypatch):
    real = db._import_icu_check_in
    started, box = threading.Event(), {}

    def form_save():
        started.set()
        db.upsert_daily_log(DAY_ISO, GOOD, GOOD, GOOD, GOOD, 4)   # its own connection

    def slow_import(conn, dt, record):
        real(conn, dt, record)               # the import's row, transaction still open
        if dt == DAY_ISO and "t" not in box:
            box["t"] = threading.Thread(target=form_save)
            box["t"].start()
            started.wait(2)
            time.sleep(0.3)                  # the form now waits on the write lock

    monkeypatch.setattr(db, "_import_icu_check_in", slow_import)
    _sync([_rec(fatigue=4)])
    box["t"].join(10)
    row = _row()
    assert _values(row) == (GOOD, GOOD, GOOD, GOOD)
    assert row["icu_fields"] is None, "the rider's save must own the day"
    _sync([_rec(fatigue=4)])
    assert _row()["fatigue"] == GOOD


# ── L3 (MT5): migration and an unmigrated database ───────────────────────────

_LEGACY_DAILY_LOG = """CREATE TABLE daily_log (
    date TEXT PRIMARY KEY, sleep_quality INTEGER, fatigue INTEGER,
    soreness INTEGER, stress INTEGER, mood INTEGER, readiness_to_train INTEGER,
    hooper_index REAL, notes TEXT, created_at TEXT DEFAULT (datetime('now')))"""


def test_mt5_the_migration_adds_the_column_and_old_rows_stay_the_riders(tmp_path, freeze_clock):
    freeze_clock(DAY)
    path = tmp_path / "legacy.db"
    c = sqlite3.connect(str(path))
    c.execute(_LEGACY_DAILY_LOG)
    c.execute("INSERT INTO daily_log (date, sleep_quality, fatigue, soreness, stress, mood,"
              " hooper_index) VALUES (?, 3, 3, 3, 3, 4, 12)", (DAY_ISO,))
    c.commit()
    c.close()
    original = db.DB_PATH
    try:
        db.set_db_path(path)
        db.init_db()
        db._sync_stop.clear()
        assert db._daily_log_has_icu_fields(db.get_db())
        assert _row()["icu_fields"] is None
        _sync([_rec(fatigue=4)])
        assert _row()["fatigue"] == GOOD, "a pre-3.14 check-in is the rider's"
    finally:
        db.set_db_path(original)


def test_mt5_without_the_column_one_warning_per_pass_and_no_write(tmp_path, freeze_clock, caplog):
    freeze_clock(DAY)
    path = tmp_path / "unmigrated.db"
    original = db.DB_PATH
    try:
        db.set_db_path(path)
        db.init_db()
        db.get_db().execute("DROP TABLE daily_log")
        db.get_db().execute(_LEGACY_DAILY_LOG)
        db.get_db().commit()
        db._sync_stop.clear()
        caplog.set_level(logging.WARNING, logger="db")
        _sync([_rec(day=(DAY - timedelta(days=i)).isoformat(), fatigue=3) for i in range(5)])
        warnings = [r for r in caplog.records if "check-in" in r.getMessage()]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        assert db.get_db().execute("SELECT COUNT(*) FROM daily_log").fetchone()[0] == 0
        assert db.get_db().execute("SELECT COUNT(*) FROM wellness").fetchone()[0] == 5
    finally:
        db.set_db_path(original)


# ── T16: the UI says it plainly ──────────────────────────────────────────────

def test_t16_the_card_and_the_verdict_name_intervals_icu():
    src = DASH.read_text(encoding="utf-8")
    for needle in ("From intervals.icu", "counted as ${counted}",
                   "From your intervals.icu check-in", "is not a check-in answer",
                   "soreness (from your injury)", "In intervals.icu today"):
        assert needle in src, needle
    start = src.index("async function loadMorningLog()")
    body = src[start:src.index("\n}\n", start)]
    assert "_morningIcuLines(d)" in body


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_t16_the_card_lines_render_what_the_api_says():
    from test_331_surfaces import _extract_js_function, _run_node
    src = DASH.read_text(encoding="utf-8")
    harness = ("const esc = s => String(s);\n" + _extract_js_function(src, "_morningIcuLines")
               + """
const a = _morningIcuLines({
  sources: {sleep_quality: 'default', fatigue: 'intervals.icu', stress: 'default', soreness: 'intervals.icu'},
  band: {band: 'tier_down', mode: 'fixed'}, icu_sleep_from_watch: true,
  icu_soreness_from_injury: true, icu_extras: {mood: 'Good', injury: 'Injured'}});
if (!a.includes('From intervals.icu: energy (fatigue), soreness (from your injury)')) throw new Error(a);
if (!a.includes('counted as Normal: stress')) throw new Error(a);
if (a.includes('counted as Normal: sleep')) throw new Error('sleep listed twice: ' + a);
if (!a.includes("Sleep: your watch's sleep score is not a check-in answer, so sleep counts as Normal.")) throw new Error(a);
if (!a.includes('In intervals.icu today: mood Good · injury Injured')) throw new Error(a);
const b = _morningIcuLines({sources: {sleep_quality: 'default', fatigue: 'intervals.icu',
  stress: 'default', soreness: 'default'}, band: {band: 'normal', mode: 'personal'}, icu_extras: {}});
if (!b.includes('counted as your usual answer')) throw new Error(b);
const c = _morningIcuLines({sources: {sleep_quality: 'manual', fatigue: 'manual', stress: 'manual',
  soreness: 'manual'}, band: {band: 'normal', mode: 'fixed'}, icu_extras: {}});
if (c !== '') throw new Error('a manual day shows intervals.icu lines: ' + c);
console.log('OK');
""")
    _run_node(harness)


# ═══ Wave 4 (3.14.0 fixes) ════════════════════════════════════════════════════

_REAL_WITH_DATA_STATUS = app_module._readiness_with_data_status
_NO_READINESS = {"score": None, "status": "INSUFFICIENT_DATA", "advice": "",
                 "components": {}, "missing": ["hrv", "tsb", "subjective", "sleep", "rhr"]}


def _readiness_with(training, sleep):
    with patch.object(app_module, "cached",
                      side_effect=lambda k, fn: training if k == "training" else sleep), \
         patch.object(app_module, "_local_sleep_metrics", return_value={}), \
         patch.object(app_module, "_merge_training_load", return_value=dict(training)), \
         patch.object(app_module, "_recent_dfa_and_decoupling",
                      return_value=([], None, None, None)):
        r = TestClient(app_module.app).get("/api/readiness")
    assert r.status_code == 200, r.text
    return r.json()


def _plan_today(monkeypatch, tmp_path, rows):
    """A stored plan whose today holds ``rows`` (each merged into a session)."""
    d = tmp_path / "plans"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(tp, "PLAN_DIR", d)
    base = {"day": DAY_ISO, "day_name": DAY.strftime("%A"), "session_type": "vo2max",
            "duration_min": 60, "tss_estimate": 80, "description": "", "zwo_file": "",
            "status": "pending"}
    week = {"week_num": 1, "start": (DAY - timedelta(days=DAY.weekday())).isoformat(),
            "end": (DAY + timedelta(days=6 - DAY.weekday())).isoformat(), "phase": "base",
            "tss_target": 400, "is_stepback": False,
            "sessions": [dict(base, **r) for r in rows]}
    (d / "current_plan.json").write_text(
        json.dumps({"goal": {"type": "general"}, "phases": [], "weeks": [week]}), encoding="utf-8")


_OWN = {"name": "Club ride", "intensity": "hard", "load_from": "intensity", "source": "manual",
        "icu_event_id": None, "icu_day": None, "replaced": None, "added_at": "2026-11-09T08:00:00"}


# ── O9: no readiness data means no readiness rule ────────────────────────────

@pytest.mark.parametrize("readiness", [
    {"score": None},
    {"score": 50, "status": "INSUFFICIENT_DATA", "data_status": "insufficient_data"},
    {"score": 50, "data_status": "insufficient_data"},
], ids=["missing", "stand-in-from-status", "stand-in-from-data-status"])
def test_o9_no_readiness_data_leaves_the_session_as_planned(today, readiness):
    planned = tp.PlannedSession(day=DAY, day_name="", session_type="vo2max",
                                duration_min=60, tss_estimate=80, description="")
    adj, reason = tp.adjust_today_session(planned, readiness, rides_recent=[],
                                          daily_log_today={}, hooper_history=[])
    assert (adj.session_type, reason) == ("vo2max", ""), reason


def test_o9_a_real_low_score_still_eases(today):
    planned = tp.PlannedSession(day=DAY, day_name="", session_type="vo2max",
                                duration_min=60, tss_estimate=80, description="")
    adj, _ = tp.adjust_today_session(planned, {"score": 50, "status": "MATIG", "data_status": "ok"},
                                     rides_recent=[], daily_log_today={}, hooper_history=[])
    assert adj.session_type == "z2"


class TestNoReadinessDataThroughTodaySession(_IntegrationBase):
    """O9 end to end: the stand-in 50 /api/today-session fills in is no reading."""

    def setUp(self):
        super().setUp()
        self._o9 = [patch.object(app_module, "compute_readiness", return_value=dict(_NO_READINESS)),
                    patch.object(app_module, "_readiness_with_data_status",
                                 side_effect=_REAL_WITH_DATA_STATUS)]
        for p in self._o9:
            p.start()

    def tearDown(self):
        for p in reversed(self._o9):
            p.stop()
        super().tearDown()

    def test_the_hard_session_stays_as_planned(self):
        with patch.object(app_module, "_load_all_rides_safe", return_value=[]):
            out = self.client.get("/api/today-session").json()
        self.assertEqual(out["adjusted"]["session_type"], "vo2max", out.get("reason"))
        self.assertFalse(out["was_modified"])
        self.assertEqual(out["reason"], "")


def test_o9_readiness_says_whether_it_had_data(today):
    assert _readiness_with(_FAKE_TRAINING, _FAKE_SLEEP)["readiness_data"] is True
    assert _readiness_with({}, {})["readiness_data"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("has_data", [False, True])
def test_o9_the_verdict_says_there_is_not_enough_data(has_data):
    from test_331_surfaces import _extract_js_function, _run_node
    src = DASH.read_text(encoding="utf-8")
    harness = """
const host = {innerHTML: ''};
const document = {getElementById: id => id === 'today-verdict' ? host : null};
const window = {};
function esc(s) { return String(s); }
async function getResource(url) {
  return {ok: true, json: async () => ({severity: 'normal', source: 'insufficient',
          readiness_data: %s, score_0_100: 50,
          severity_reasons: ['Insufficient wellness history for auto-adjust']})};
}
%s
loadReadinessComposite().then(() => {
  const h = host.innerHTML;
  if (%s) {
    if (!h.includes('Ready to train') || !h.includes('50<span')) throw new Error(h);
  } else {
    if (!h.includes('Not enough data') || h.includes('Ready to train')) throw new Error(h);
    if (!h.includes("today's session stays as planned")) throw new Error(h);
    if (h.includes('50<span') || !h.includes('—<span')) throw new Error('the stand-in 50 is shown: ' + h);
  }
  console.log('OK');
});
""" % ("true" if has_data else "false", _extract_js_function(src, "loadReadinessComposite"),
       "true" if has_data else "false")
    _run_node(harness)


# ── QA-INT page-load race: the server says whether today is an own ride ──────

def test_race_readiness_says_whether_today_is_the_riders_own_ride(today, monkeypatch, tmp_path):
    _plan_today(monkeypatch, tmp_path, [{"own": dict(_OWN), "session_type": "sweetspot"}])
    assert _readiness()["today_is_own"] is True
    _plan_today(monkeypatch, tmp_path, [{}])
    assert _readiness()["today_is_own"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("own", [True, False])
def test_race_ease_today_hides_on_the_servers_word_alone(own):
    from test_331_surfaces import _extract_js_function, _run_node
    src = DASH.read_text(encoding="utf-8")
    harness = """
const host = {innerHTML: ''};
const document = {getElementById: id => id === 'today-verdict' ? host : null};
const window = {};          // loadTodaySession has not answered yet
function esc(s) { return String(s); }
async function getResource(url) {
  return {ok: true, json: async () => ({severity: 'tier_down', source: 'hooper',
          readiness_data: true, today_is_own: %s, score_0_100: 62,
          severity_reasons: ['Your check-in is below your usual.']})};
}
%s
loadReadinessComposite().then(() => {
  const ease = host.innerHTML.includes('apply-tier-down-btn');
  if (ease === %s) throw new Error('Ease today shown: ' + ease);
  if (!host.innerHTML.includes('apply-week-tier-down-btn')) throw new Error('week button gone');
  console.log('OK');
});
""" % ("true" if own else "false", _extract_js_function(src, "loadReadinessComposite"),
       "true" if own else "false")
    _run_node(harness)


# ── QX-M1: the injury line says what happens to today ────────────────────────

@pytest.mark.parametrize("row,expect,never", [
    ({"own": dict(_OWN), "session_type": "tempo"}, "Your own ride stays as you planned it",
     "recovery ride"),
    ({"is_race": True, "session_type": "recovery", "description": "B RACE"},
     "Your race stays as planned", "recovery ride"),
    ({"session_type": "rest", "duration_min": 0, "tss_estimate": 0}, "Today is a rest day",
     "recovery ride"),
    ({"session_type": "recovery", "duration_min": 40}, "already an easy ride", "becomes"),
    ({"session_type": "vo2max"}, "so today's session becomes a short recovery ride", None),
], ids=["own-ride", "race", "rest", "recovery", "planned"])
def test_qx_m1_the_injury_line_says_what_happens_today(today, monkeypatch, tmp_path,
                                                      row, expect, never):
    _plan_today(monkeypatch, tmp_path, [row])
    _sync([_rec(injury=3)])
    note = _readiness()["injury_note"]
    assert expect in note, note
    assert never is None or never not in note, note


def test_qx_m1_keep_original_is_kept(today, monkeypatch, tmp_path):
    _plan_today(monkeypatch, tmp_path, [{"session_type": "vo2max"}])
    _sync([_rec(injury=4)])
    monkeypatch.setattr(app_module, "_is_readiness_cap_reverted_today", lambda: True)
    note = _readiness()["injury_note"]
    assert "you chose to keep today's session as planned" in note and "becomes" not in note


def test_qx_m1_no_session_today_reads_as_a_rest_day(today, monkeypatch, tmp_path):
    _plan_today(monkeypatch, tmp_path, [{"day": "2026-11-11"}])
    _sync([_rec(injury=3)])
    assert "Today is a rest day" in _readiness()["injury_note"]


# ── QX-M6: the recovery gate in plain words, naming an imported injury ───────

def test_qx_m6_an_imported_injury_is_named_not_sore_legs(today):
    _sync([_rec(fatigue=1, injury=3)])
    planned = tp.PlannedSession(day=DAY, day_name="", session_type="vo2max",
                                duration_min=60, tss_estimate=80, description="")
    adj, reason = tp.adjust_today_session(planned, {"score": 90, "dfa_cap": {}},
                                          rides_recent=[], daily_log_today=_row(),
                                          hooper_history=[])
    assert adj.session_type == "recovery"
    assert reason == ("You logged an injury in intervals.icu (Poor) — today's session is a "
                      "short recovery ride."), reason
    assert adj.description == "Recovery ride — the injury you logged in intervals.icu."


def test_qx_m6_sore_legs_in_plain_words(today):
    db.upsert_daily_log(DAY_ISO, NORMAL, NORMAL, TIRED, NORMAL, 4)        # soreness Sore
    planned = tp.PlannedSession(day=DAY, day_name="", session_type="threshold",
                                duration_min=60, tss_estimate=80, description="")
    adj, reason = tp.adjust_today_session(planned, {"score": 90, "dfa_cap": {}},
                                          rides_recent=[], daily_log_today=_row(),
                                          hooper_history=[])
    assert adj.session_type == "recovery"
    assert reason == "Sore legs this morning — today's session is a short recovery ride."
    assert adj.description == "Recovery ride — sore legs today."
    for code in ("G5", "6/7", "peripheral", "Hooper", "Cheung"):
        assert code not in reason + adj.description, code


# ── QX-L3 / QX-L4 / QX-L13: the card's lines ─────────────────────────────────

def test_qx_l4_the_api_says_when_soreness_came_from_the_injury(today):
    _sync([_rec(fatigue=1, injury=3)])
    d = TestClient(app_module.app).get("/api/daily-log").json()
    assert d["icu_soreness_from_injury"] is True
    _sync([_rec(soreness=3, injury=3)])                 # Sore answered anyway
    d = TestClient(app_module.app).get("/api/daily-log").json()
    assert d["icu_soreness_from_injury"] is False


def test_qx_l13_a_riders_own_check_in_says_intervals_icus_answers_are_not_used(today):
    db.upsert_daily_log(DAY_ISO, GOOD, GOOD, GOOD, GOOD, 4)
    _sync([_rec(fatigue=3)])
    d = TestClient(app_module.app).get("/api/daily-log").json()
    assert set(d["sources"].values()) == {"manual"} and d["icu_answers_today"] is True


def test_qx_l13_the_form_says_saving_makes_the_day_yours():
    src = DASH.read_text(encoding="utf-8")
    start = src.index("function showMorningForm(stored)")
    body = src[start:src.index("\n}\n", start)]
    assert "Saving here replaces today's answers from intervals.icu." in body
    assert "window._todayIcuImported" in body
    assert ">Morning check-in<" in body and "leg-check" not in body


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_qx_l3_l4_l13_the_card_lines():
    from test_331_surfaces import _extract_js_function, _run_node
    src = DASH.read_text(encoding="utf-8")
    harness = ("const esc = s => String(s);\n" + _extract_js_function(src, "_morningIcuLines")
               + """
const a = _morningIcuLines({sources: {sleep_quality: 'manual', fatigue: 'manual',
  stress: 'manual', soreness: 'manual'}, icu_answers_today: true,
  icu_extras: {injury: 'None', mood: 'OK'}});
if (!a.includes('Your answers in intervals.icu today are not used: you checked in here.')) throw new Error(a);
if (!a.includes('injury') || a.includes('injury None')) throw new Error(a);
if (!a.includes('mood OK · no injury')) throw new Error(a);
const b = _morningIcuLines({sources: {sleep_quality: 'default', fatigue: 'intervals.icu',
  stress: 'default', soreness: 'default'}, band: {mode: 'personal'}, icu_sleep_from_watch: true,
  icu_extras: {}});
if (!b.includes('counted as your usual answer: stress, soreness')) throw new Error(b);
if (!b.includes('so sleep counts as your usual answer.')) throw new Error(b);
const c = _morningIcuLines({sources: {sleep_quality: 'manual', fatigue: 'manual',
  stress: 'manual', soreness: 'manual'}, icu_answers_today: false, icu_extras: {}});
if (c !== '') throw new Error(c);
console.log('OK');
""")
    _run_node(harness)
