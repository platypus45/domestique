"""3.14.0 — the recalibrated morning check-in band (grill_wellness Part B,
Design B; owner decision 2026-11-10).

The fixed Hooper bands (14 ease off, 18 rest + Z2 cap) had no traceable
source and read Domestique's ordinary morning as fatigue: the form starts
every question at "Normal" (stored 4), so an untouched check-in is 16 and
said "Ease off today", and one answer a notch worse capped hard sessions.
The band now compares with the rider's own normal once 14 check-ins exist in
the 28 days before the day, and uses fixed limits on the form's neutral until
then. One owner, readiness_composite.hooper_band; every reader asks it.

Rules for this file: no threshold is written out. Every number comes from
readiness_composite's constants or from the form's own button values
(db._FORM_STORE), and fixture invariants assert that a case sits where its
name says it sits.

RB1–RB18 follow the grill's list; each fails without its rule.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db                                    # noqa: E402
import readiness_composite as rc             # noqa: E402
import training_planner as tp                # noqa: E402

# The form's buttons, as the form stores them (level 1..5 -> 1..7).
GREAT, GOOD, NORMAL, TIRED, WORST = (db._FORM_STORE[k] for k in (1, 2, 3, 4, 5))
assert NORMAL == rc.HOOPER_NORMAL_ANSWER
assert TIRED == rc.HOOPER_NEGATIVE_ANSWER
assert WORST == rc.HOOPER_WORST_ANSWER

DAY = date(2026, 11, 10)
DAY_ISO = DAY.isoformat()
COLS = db.HOOPER_COLS


def _row(day, sleep_quality=NORMAL, fatigue=NORMAL, stress=NORMAL,
         soreness=NORMAL, icu_fields=None):
    d = day.isoformat() if isinstance(day, date) else day
    vals = {"sleep_quality": sleep_quality, "fatigue": fatigue,
            "stress": stress, "soreness": soreness}
    return {"date": d, **vals, "hooper_index": sum(vals.values()),
            "icu_fields": icu_fields, "mood": 4}


def _imported(day, **answers):
    """A day imported from intervals.icu: unanswered questions stored Normal."""
    return _row(day, **answers, icu_fields=",".join(sorted(answers)))


def _days_before(n, start=1):
    return [DAY - timedelta(days=start + i) for i in range(n)]


def _usually_good(n=rc.HOOPER_BASELINE_DAYS):
    """A rider whose ordinary morning is all "Good", with a little spread."""
    pats = [dict(sleep_quality=GOOD, fatigue=GOOD, stress=GOOD, soreness=GOOD),
            dict(sleep_quality=GOOD, fatigue=NORMAL, stress=GOOD, soreness=GOOD),
            dict(sleep_quality=GOOD, fatigue=GOOD, stress=GOOD, soreness=GOOD),
            dict(sleep_quality=GOOD, fatigue=GOOD, stress=NORMAL, soreness=GOOD),
            dict(sleep_quality=GOOD, fatigue=GOOD, stress=GOOD, soreness=GOOD),
            dict(sleep_quality=GOOD, fatigue=GOOD, stress=GOOD, soreness=GREAT)]
    return [_row(d, **pats[i % len(pats)]) for i, d in enumerate(_days_before(n))]


def _band(today, history):
    return rc.hooper_band(DAY_ISO, today_row=today, history=history)


def _planned(stype="vo2max"):
    return tp.PlannedSession(day=DAY, day_name=DAY.strftime("%A").lower(),
                             session_type=stype, duration_min=60,
                             tss_estimate=90, description=f"Planned {stype}")


def _adjust(today, history, stype="vo2max"):
    return tp.adjust_today_session(_planned(stype), {"score": 90, "dfa_cap": {}},
                                   rides_recent=[], daily_log_today=today,
                                   hooper_history=history)


# ── fixed limits (fewer than 14 check-ins) ───────────────────────────────────

def test_rb1_all_normal_is_a_normal_day_and_never_capped():
    today = _row(DAY)
    assert sum(today[c] for c in COLS) < rc.HOOPER_FIXED_TIER_DOWN
    b = _band(today, [])
    assert b["band"] == "normal" and b["mode"] == "fixed"
    adj, reason = _adjust(today, [])
    assert adj.session_type == "vo2max" and reason == ""


def test_rb2_one_tired_answer_eases_off_but_does_not_cap():
    today = _row(DAY, fatigue=TIRED)
    s = sum(today[c] for c in COLS)
    assert rc.HOOPER_FIXED_TIER_DOWN <= s < rc.HOOPER_FIXED_REST
    assert _band(today, [])["band"] == "tier_down"
    adj, _ = _adjust(today, [])
    assert adj.session_type == "vo2max", "an easing advice must never cap"


def test_rb3_two_tired_answers_rest_and_cap():
    today = _row(DAY, sleep_quality=TIRED, fatigue=TIRED)
    assert sum(today[c] for c in COLS) >= rc.HOOPER_FIXED_REST
    assert _band(today, [])["band"] == "rest"
    adj, reason = _adjust(today, [])
    assert adj.session_type == "z2"
    assert "check-in" in reason


def test_rb4_an_answer_at_the_worst_end_rests_even_on_a_low_sum():
    today = _row(DAY, sleep_quality=WORST, fatigue=GOOD, stress=GOOD, soreness=GOOD)
    assert sum(today[c] for c in COLS) < rc.HOOPER_FIXED_TIER_DOWN
    assert _band(today, [])["band"] == "rest"
    adj, _ = _adjust(today, [])
    assert adj.session_type == "z2"


# ── personal mode ────────────────────────────────────────────────────────────

def test_rb5_personal_mode_starts_at_the_minimum_number_of_check_ins():
    need = rc.HOOPER_PERSONAL_MIN_CHECKINS
    today = _row(DAY)
    fixed = _band(today, _usually_good(need - 1))
    personal = _band(today, _usually_good(need))
    assert fixed["mode"] == "fixed" and fixed["band"] == "normal"
    assert personal["mode"] == "personal"
    # For a rider whose normal is "Good", an all-Normal morning is an
    # advisory below their usual, never a cap (no answer is negative).
    assert personal["band"] == "tier_down"


def test_rb6_one_tired_answer_against_a_good_normal_rests_but_an_import_does_not():
    hist = _usually_good()
    form = _row(DAY, fatigue=TIRED)
    assert _band(form, hist)["band"] == "rest"
    adj, _ = _adjust(form, hist)
    assert adj.session_type == "z2"
    # The same answer imported alone: the unanswered questions take the
    # rider's usual answer ("Good"), not Normal, so it is an easing advice.
    imp = _imported(DAY, fatigue=TIRED)
    b = _band(imp, hist)
    assert b["band"] == "tier_down", b
    adj, _ = _adjust(imp, hist)
    assert adj.session_type == "vo2max"


def test_rb7_a_low_fatigue_import_is_a_normal_day_for_a_good_rider():
    b = _band(_imported(DAY, fatigue=GOOD), _usually_good())
    assert b["mode"] == "personal" and b["band"] == "normal", b


def test_rb8_a_chronic_bad_normal_is_capped_as_the_centre():
    chronic = dict(sleep_quality=TIRED, fatigue=TIRED)
    hist = [_row(d, **chronic) for d in _days_before(rc.HOOPER_BASELINE_DAYS)]
    s = sum(_row(DAY, **chronic)[c] for c in COLS)
    assert s > rc.HOOPER_CENTRE_CAP, "fixture: the chronic normal sits above the cap"
    b = _band(_row(DAY, **chronic), hist)
    assert b["usual"] == rc.HOOPER_CENTRE_CAP
    assert b["band"] == "tier_down", "a chronic Tired + Poor must never read as normal"


def test_rb9_the_spread_is_capped_and_floored():
    # Volatile: half the mornings all Good, half Tired + Poor.
    bad = dict(sleep_quality=TIRED, fatigue=TIRED)
    hist = [_row(d, **(bad if i % 2 else {c: GOOD for c in COLS}))
            for i, d in enumerate(_days_before(rc.HOOPER_BASELINE_DAYS))]
    b = _band(_row(DAY, **bad), hist)
    assert b["mode"] == "personal"
    assert b["band"] == "tier_down", (
        "without the spread cap a volatile rider's Tired + Poor reads normal")
    # Never varies: the spread floor stops a division by zero turning one
    # notch worse into rest.
    flat = [_row(d) for d in _days_before(rc.HOOPER_BASELINE_DAYS)]
    b = _band(_row(DAY, fatigue=TIRED), flat)
    assert b["band"] == "tier_down", b


def test_rb10_the_ceiling_rests_in_both_modes():
    three = dict(sleep_quality=TIRED, fatigue=TIRED, stress=TIRED)
    today = _row(DAY, **three)
    assert sum(today[c] for c in COLS) >= rc.HOOPER_CEILING
    assert _band(today, [])["band"] == "rest"
    # A volatile rider whose usual sits at the centre cap: z alone would only
    # advise easing; the ceiling still rests.
    lo = dict(fatigue=NORMAL, soreness=NORMAL, sleep_quality=GOOD, stress=GOOD)
    hist = [_row(d, **(three if i % 2 else lo))
            for i, d in enumerate(_days_before(rc.HOOPER_BASELINE_DAYS))]
    b = _band(today, hist)
    assert b["mode"] == "personal"
    assert b["z"] < rc.HOOPER_Z_REST, "fixture: z alone would not rest"
    assert b["band"] == "rest"
    adj, _ = _adjust(today, hist)
    assert adj.session_type == "z2"


def test_rb11_personal_mode_is_offset_invariant():
    """Whether intervals.icu's best answer is "None" (stored Great) or "Low"
    (stored Good) shifts a question by a constant: the band must not care."""
    hist = _usually_good()
    shift = GOOD - GREAT
    shifted = [dict(r, soreness=r["soreness"] + shift) for r in hist]
    cases = [dict(soreness=GREAT), dict(fatigue=TIRED, soreness=GREAT),
             dict(sleep_quality=TIRED, fatigue=TIRED, soreness=GREAT),
             dict(sleep_quality=NORMAL, fatigue=NORMAL, stress=NORMAL, soreness=GREAT)]
    for c in cases:
        a = _band(_row(DAY, **c), hist)
        b = _band(_row(DAY, **dict(c, soreness=c["soreness"] + shift)), shifted)
        assert a["band"] == b["band"], (c, a, b)


def test_rb12_today_and_old_rows_stay_out_of_the_baseline():
    need = rc.HOOPER_PERSONAL_MIN_CHECKINS
    today = _row(DAY)
    base = _usually_good(need - 1)
    assert _band(today, base + [_row(DAY)])["mode"] == "fixed", "today counted itself"
    edge = DAY - timedelta(days=rc.HOOPER_BASELINE_DAYS)
    assert _band(today, base + [_row(edge)])["mode"] == "personal", (
        "the first day of the window must count")
    old = [_row(edge - timedelta(days=i + 1)) for i in range(5)]
    assert _band(today, base + old)["mode"] == "fixed", "rows before the window counted"


def test_rb13_form_and_imported_days_both_count_and_imports_use_only_their_answers():
    need = rc.HOOPER_PERSONAL_MIN_CHECKINS
    days = _days_before(need)
    half = need // 2
    form = [_row(d, sleep_quality=GREAT, fatigue=GOOD, stress=GOOD, soreness=GOOD)
            for d in days[:half]]
    imp = [_imported(d, fatigue=GOOD) for d in days[half:]]
    assert _band(_row(DAY), form)["mode"] == "fixed"
    b = _band(_imported(DAY, fatigue=TIRED), form + imp)
    assert b["mode"] == "personal", "imported days must count as check-ins"
    # The usual sleep answer is the rider's own "Great" (form days only): the
    # imported days never answered sleep, their stored Normal is no answer.
    assert b["compared"] == GREAT + TIRED + GOOD + GOOD, b


# ── readers ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def fresh_db(tmp_path):
    original = db.DB_PATH
    db.set_db_path(tmp_path / "bands.db")
    db.init_db()
    try:
        yield
    finally:
        db.set_db_path(original)


def _put(day, **vals):
    v = {c: vals.get(c, NORMAL) for c in COLS}
    db.upsert_daily_log(day.isoformat(), v["sleep_quality"], v["fatigue"],
                        v["soreness"], v["stress"], 4)


_FAKE_TRAINING = {"ctl": 50.0, "atl": 50.0, "tsb": 0.0}
_FAKE_SLEEP = {"ln_rmssd_7d": 3.5, "swc_lower": 3.4, "swc_upper": 3.6,
               "sleep_h": 7.5, "rhr_delta": 0.0}


def _readiness_score(client, app_module):
    with patch.object(app_module, "cached",
                      side_effect=lambda k, fn: _FAKE_TRAINING if k == "training" else _FAKE_SLEEP), \
         patch.object(app_module, "_local_sleep_metrics", return_value={}), \
         patch.object(app_module, "_merge_training_load", return_value=dict(_FAKE_TRAINING)), \
         patch.object(app_module, "_recent_dfa_and_decoupling",
                      return_value=([], None, None, None)):
        r = client.get("/api/readiness")
    assert r.status_code == 200, r.text
    return r.json()["score_0_100"]


def test_rb14_an_all_normal_check_in_leaves_readiness_where_no_check_in_does(
        fresh_db, freeze_clock):
    freeze_clock(DAY)
    from fastapi.testclient import TestClient
    import app as app_module
    client = TestClient(app_module.app)
    none = _readiness_score(client, app_module)
    _put(DAY)
    normal = _readiness_score(client, app_module)
    assert normal == none, "answering Normal must not lower readiness (it read 50/100)"
    _put(DAY, sleep_quality=GOOD, fatigue=GOOD, stress=GOOD, soreness=GOOD)
    assert _readiness_score(client, app_module) == none, (
        "better than Normal does not raise it either")
    _put(DAY, fatigue=TIRED)
    assert _readiness_score(client, app_module) < none, "one Tired still lowers it"


def test_rb15_soreness_at_sore_still_forces_recovery_in_both_modes():
    sore = _row(DAY, soreness=TIRED)
    adj, reason = _adjust(sore, [])
    # 3.14.0 wave 4 (QX-M6): the recovery gate speaks plainly now.
    assert adj.session_type == "recovery" and "Sore legs this morning" in reason
    chronic_sore = [_row(d, soreness=TIRED) for d in _days_before(rc.HOOPER_BASELINE_DAYS)]
    assert _band(sore, chronic_sore)["mode"] == "personal"
    adj, reason = _adjust(sore, chronic_sore)
    assert adj.session_type == "recovery", "the soreness gate is not personal"


def test_rb16_severity_keeps_six_keys_and_speaks_plainly(fresh_db, freeze_clock):
    freeze_clock(DAY)
    cases = [dict(), dict(fatigue=TIRED), dict(sleep_quality=TIRED, fatigue=TIRED),
             dict(soreness=WORST)]
    for c in cases:
        _put(DAY, **c)
        out = rc.compute_training_severity("rider", DAY_ISO)
        assert set(out) == {"score", "severity", "source", "reasons",
                            "hooper_index", "tsb"}
        assert out["source"] == "hooper"
        assert out["hooper_index"] == sum({**{k: NORMAL for k in COLS}, **c}.values())
        for reason in out["reasons"]:
            for bad in ("_", "≥", ">", "<", "Hooper", "tier"):
                assert bad not in reason, (bad, reason)
    for d in _days_before(rc.HOOPER_PERSONAL_MIN_CHECKINS):
        _put(d, sleep_quality=GOOD, fatigue=GOOD, stress=GOOD, soreness=GOOD)
    _put(DAY)
    out = rc.compute_training_severity("rider", DAY_ISO)
    assert out["severity"] == "tier_down" and "usual" in out["reasons"][0]


def test_rb17_the_morning_card_colours_from_the_server_band():
    src = (ROOT / "src" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    start = src.index("async function loadMorningLog()")
    body = src[start:src.index("\n}\n", start)]
    assert "d.band" in body
    for literal in (">= 18", ">= 14", ">=18", ">=14"):
        assert literal not in body, f"fixed cut-off {literal!r} back in the card"


def test_rb18_the_summary_chart_draws_the_fixed_limit_not_the_old_cap():
    from PIL import Image, ImageDraw
    import programme_summary_png as psp
    img = Image.new("RGB", (600, 300))
    draw = ImageDraw.Draw(img)
    texts = []
    real_text = draw.text

    def _spy(xy, text, *a, **k):
        texts.append(text)
        return real_text(xy, text, *a, **k)

    draw.text = _spy
    psp._draw_hooper(draw, 0, 0, 600, 300,
                     [{"week": i + 1, "mean": 14.0 + i} for i in range(6)])
    assert "Hooper=18 (cap)" not in texts
    assert f"fixed limit {rc.HOOPER_FIXED_REST}" in texts
