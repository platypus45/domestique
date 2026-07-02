"""P2.1 (v3.0.0, G10) — execution scoring: pure module + wiring + survival.

Locks:
  * axis weights 0.3/0.3/0.4 (renormalized when axes drop out);
  * components capped at 1 for the SCORE, verdicts from UNCAPPED ratios;
  * verdict thresholds (off_plan <0.40, under <0.80, over >1.25);
  * the session_type→zone-bucket mapping table (sweetspot straddles z3/z4,
    overunder straddles z4/z5, "ss" accumulator excluded);
  * hr mode maps prescription rows→ICU HR-zone frame with ±1-bucket tolerance;
  * sprint (RPE-only) scores duration+load with basis="load_only";
  * basis follows DATA present (power TiZ scores on power even in hr mode);
  * wiring: _apply_rematch_preview_to_plan persists `execution` from the
    RE-FETCHED full ride, recomputes when the match entry changes;
  * `execution` is a PlannedSession dataclass field + SESSION_FIELDS_LOCKED;
  * the score survives regenerate/refit/reforecast round-trips (dict-level).
"""
from __future__ import annotations

import dataclasses
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
import execution_score as es  # noqa: E402
import training_planner as tp  # noqa: E402


def _tiz(**kw):
    t = {f"z{i}": 0 for i in range(1, 8)}
    t.update(kw)
    return t


def _planned(stype="threshold", dur=60, tss=80):
    return {"session_type": stype, "duration_min": dur, "tss_estimate": tss}


def _ride(dur_s=3600, tss=80, tiz=None, hr_tiz=None):
    r = {"duration_s": dur_s, "tss": tss}
    if tiz is not None:
        r["time_in_zone"] = tiz
    if hr_tiz is not None:
        r["hr_time_in_zone"] = hr_tiz
    return r


# ── locked constants + mapping table ─────────────────────────────────────────

def test_locked_weights_and_verdict_thresholds():
    assert es.WEIGHTS == {"duration": 0.3, "load": 0.3, "intensity": 0.4}
    assert es.VERDICT_OFF_PLAN_BELOW == 0.40
    assert es.VERDICT_UNDER_BELOW == 0.80
    assert es.VERDICT_OVER_ABOVE == 1.25
    assert es.HR_BUCKET_TOLERANCE == 1


def test_locked_band_table():
    """The session_type→zone-bucket mapping is a contract (G10)."""
    assert es.POWER_BANDS["sweetspot"][0] == (3, 4)   # straddle decision
    assert es.POWER_BANDS["overunder"][0] == (4, 5)   # straddle decision
    assert es.POWER_BANDS["threshold"][0] == (4,)
    assert es.POWER_BANDS["tempo"][0] == (3,)
    assert es.POWER_BANDS["z2"][0] == (1, 2)
    assert es.POWER_BANDS["long_z2"][0] == (1, 2)
    assert es.POWER_BANDS["recovery"][0] == (1, 2)
    assert es.POWER_BANDS["vo2max"][0] == (5, 6)
    assert es.POWER_BANDS["anaerobic"][0] == (5, 6, 7)
    assert es.POWER_BANDS["ftp_test"][0] == (4, 5)
    for _zones, expected in es.POWER_BANDS.values():
        assert 0 < expected <= 1
    assert "sprint" in es.RPE_ONLY_TYPES
    # Aliases normalize to canonical keys.
    assert es.TYPE_ALIASES["sweet_spot"] == "sweetspot"
    assert es.TYPE_ALIASES["over_under"] == "overunder"
    assert es.TYPE_ALIASES["neuromuscular"] == "sprint"


# ── per-axis behaviour ───────────────────────────────────────────────────────

def test_perfect_threshold_ride_scores_100():
    tiz = _tiz(z1=600, z2=600, z3=400, z4=1800, z5=200)  # 0.50 z4 fraction
    r = es.score_ride(_planned(), _ride(tss=80, tiz=tiz), "power")
    assert r["basis"] == "power"
    assert r["score"] == 100
    assert r["verdict"] == "on_target"


def test_duration_axis_uncapped_ratio_capped_score():
    tiz = _tiz(z1=600, z2=600, z4=1800, z5=600)
    r = es.score_ride(_planned(dur=60), _ride(dur_s=2 * 3600, tss=80, tiz=tiz),
                      "power")
    d = r["components"]["duration"]
    assert d["ratio"] == 2.0          # uncapped
    assert d["score"] == 1.0          # capped for the score
    assert r["verdict"] == "over"     # verdict from the UNCAPPED ratio


def test_load_axis_under():
    tiz = _tiz(z1=900, z2=900, z4=1800)
    r = es.score_ride(_planned(tss=100), _ride(tss=60, tiz=tiz), "power")
    assert r["components"]["load"]["ratio"] == 0.6
    assert r["verdict"] == "under"


def test_off_plan_when_any_ratio_collapses():
    # 20 min ride against a 60 min plan → duration ratio 0.33 < 0.40.
    tiz = _tiz(z4=600, z1=600)
    r = es.score_ride(_planned(dur=60, tss=80),
                      _ride(dur_s=1200, tss=20, tiz=tiz), "power")
    assert r["verdict"] == "off_plan"


def test_verdict_boundaries_exact():
    """0.80 and 1.25 are inclusive on_target; 0.40 is not off_plan."""
    mk = lambda ratio: es._verdict([ratio])  # noqa: E731
    assert mk(0.40) == "under"        # not off_plan at the boundary
    assert mk(0.399) == "off_plan"
    assert mk(0.80) == "on_target"    # not under at the boundary
    assert mk(0.799) == "under"
    assert mk(1.25) == "on_target"    # not over at the boundary
    assert mk(1.251) == "over"


def test_intensity_fraction_normalized_by_expected():
    """A well-executed vo2max session (~28% in z5/z6) scores full intensity."""
    tiz = _tiz(z1=1800, z2=900, z3=200, z5=1000, z6=100)  # 1100/4000 = 0.275
    r = es.score_ride(_planned("vo2max", 65, 75), _ride(dur_s=65 * 60, tss=75,
                                                        tiz=tiz), "power")
    i = r["components"]["intensity"]
    assert i["band"] == ["z5", "z6"]
    assert i["target_fraction"] == 0.28
    assert 0.95 <= i["ratio"] <= 1.05
    assert r["verdict"] == "on_target"


def test_ss_accumulator_key_is_excluded():
    tiz = _tiz(z1=600, z4=600)
    tiz["ss"] = 99999  # overlapping accumulator must not pollute sums
    r = es.score_ride(_planned("threshold", 20, 30),
                      _ride(dur_s=1200, tss=30, tiz=tiz), "power")
    assert r["components"]["intensity"]["band_fraction"] == 0.5


# ── basis selection (G3) ─────────────────────────────────────────────────────

def test_basis_follows_power_data_even_in_hr_mode():
    tiz = _tiz(z1=600, z2=600, z4=1800, z5=200)
    r = es.score_ride(_planned(), _ride(tss=80, tiz=tiz), "hr")
    assert r["basis"] == "power"
    assert r["components"]["intensity"]["band_frame"] == "power"


def test_hr_basis_with_plus_minus_one_bucket_tolerance():
    hr = _tiz(z3=900, z4=1500, z5=600, z2=600)  # threshold day by HR
    r = es.score_ride(_planned("threshold", 60, 80),
                      _ride(tss=80, hr_tiz=hr), "power")
    assert r["basis"] == "hr"
    i = r["components"]["intensity"]
    assert i["band"] == ["z3", "z4", "z5"]  # z4 ± 1
    assert i["band_frame"] == "hr"
    # 3000/3600 in band ÷ 0.50 expected → capped 1.0
    assert i["score"] == 1.0


def test_hr_only_vo2max_is_load_only():
    """z5+ prescriptions are RPE in hr mode — HR TiZ can't grade them."""
    hr = _tiz(z2=1200, z4=1200, z5=1200)
    r = es.score_ride(_planned("vo2max", 60, 75), _ride(tss=75, hr_tiz=hr),
                      "hr")
    assert r["basis"] == "load_only"
    assert r["components"]["intensity"] is None


def test_sprint_is_always_load_only():
    tiz = _tiz(z1=2400, z2=900, z7=120)
    for mode in ("power", "hr"):
        r = es.score_ride(_planned("sprint", 60, 55),
                          _ride(dur_s=3600, tss=55, tiz=tiz), mode)
        assert r["basis"] == "load_only"
        assert r["components"]["intensity"] is None


def test_load_only_reweights_to_half_half():
    """0.3/0.3 renormalize to 0.5/0.5 when intensity is absent."""
    r = es.score_ride(_planned("sprint", 60, 50),
                      {"duration_s": 3600, "tss": 25}, "power")
    # duration 1.0, load 0.5 → 0.5*1 + 0.5*0.5 = 0.75
    assert r["score"] == 75
    assert r["basis"] == "load_only"


def test_no_tiz_falls_back_to_load_only():
    r = es.score_ride(_planned(), {"duration_s": 3600, "tss": 80}, "power")
    assert r["basis"] == "load_only"
    assert r["score"] == 100


def test_nothing_scoreable_returns_none_score():
    r = es.score_ride({"session_type": "z2"}, {}, "power")
    assert r["score"] is None
    assert r["verdict"] == "off_plan"


def test_deterministic():
    tiz = _tiz(z1=600, z2=600, z4=1800, z5=200)
    a = es.score_ride(_planned(), _ride(tss=78, tiz=tiz), "power")
    b = es.score_ride(_planned(), _ride(tss=78, tiz=tiz), "power")
    assert a == b


# ── persistence contract: dataclass field + locked set ──────────────────────

def test_execution_is_a_planned_session_field_and_locked():
    names = {f.name for f in dataclasses.fields(tp.PlannedSession)}
    assert "execution" in names
    assert "execution" in app_module.SESSION_FIELDS_LOCKED
    s = tp.PlannedSession(day=date(2026, 6, 1), day_name="Mon",
                          session_type="z2", duration_min=60,
                          tss_estimate=45, description="")
    assert s.execution is None
    j = app_module._planned_session_to_json(s)
    assert "execution" in j and j["execution"] is None
    j["execution"] = {"score": 91}
    back = app_module._planned_session_from_json(j)
    assert back.execution == {"score": 91}


# ── wiring: written at match time from the re-fetched full ride ─────────────

def _mk_week_plan(monday: date, stype="threshold", dur=60, tss=80) -> dict:
    sessions = []
    for off in range(7):
        d = monday + timedelta(days=off)
        if off == 0:
            sessions.append({"day": d.isoformat(), "day_name": "Mon",
                             "session_type": stype, "duration_min": dur,
                             "tss_estimate": tss, "description": "",
                             "zwo_file": "x.zwo", "zwo_name": "X",
                             "status": "pending"})
        else:
            sessions.append({"day": d.isoformat(), "day_name": "D",
                             "session_type": "rest", "duration_min": 0,
                             "tss_estimate": 0, "description": ""})
    return {"weeks": [{"week_num": 1, "start": monday.isoformat(),
                       "end": (monday + timedelta(days=6)).isoformat(),
                       "phase": "build1", "tss_target": 300,
                       "sessions": sessions}]}


def _preview(day_iso: str, activity_id, tss=80.0):
    return {"matches": [{
        "session_date": day_iso, "session_type": "threshold",
        "current_status": "pending", "new_status": "done",
        "matched_axes": 3, "score": 0.95,
        "axes": {"tss_ok": True, "duration_ok": True, "if_band_ok": True},
        "activity_id": activity_id,
        "details": {"tss": tss},
    }]}


def _full_ride(aid, tss=78.0, dur_s=3600, z4=1800):
    return {"ride_id": f"icu_{aid}", "source": "icu", "external_id": str(aid),
            "duration_s": dur_s, "moving_s": dur_s, "tss": tss,
            "time_in_zone": _tiz(z1=600, z2=600, z3=400, z4=z4, z5=200),
            "hr_time_in_zone": None}


@pytest.fixture
def _stub_rides(monkeypatch):
    """Point the persist step's re-fetch at synthetic full rides."""
    store: dict[str, dict] = {}
    monkeypatch.setattr(app_module, "_load_all_rides_safe",
                        lambda: list(store.values()))
    import ride_storage
    monkeypatch.setattr(ride_storage, "list_rides", lambda: [])
    return store


def test_match_apply_writes_execution(monkeypatch, _stub_rides):
    monday = date(2026, 6, 1)
    plan = _mk_week_plan(monday)
    _stub_rides["i1"] = _full_ride("i1")
    n = app_module._apply_rematch_preview_to_plan(plan, 0,
                                                  _preview(monday.isoformat(), "i1"))
    assert n == 1
    s = plan["weeks"][0]["sessions"][0]
    ex = s.get("execution")
    assert isinstance(ex, dict)
    assert ex["activity_id"] == "i1"
    assert ex["basis"] == "power"
    assert isinstance(ex["score"], int) and ex["score"] > 90
    assert "computed_at" in ex
    # The completion-match entry keeps its own locked schema.
    entry = s["completion_matches"][0]
    assert set(entry) == {"activity_id", "matched_axes", "score", "axes",
                          "details", "applied_at"}


def test_recompute_when_match_entry_changes(_stub_rides):
    """A re-match to a DIFFERENT activity replaces the score (G10)."""
    monday = date(2026, 6, 1)
    plan = _mk_week_plan(monday)
    _stub_rides["i1"] = _full_ride("i1", tss=78.0)
    app_module._apply_rematch_preview_to_plan(plan, 0,
                                              _preview(monday.isoformat(), "i1"))
    s = plan["weeks"][0]["sessions"][0]
    first = dict(s["execution"])
    # New sync matches a different (much lighter) ride.
    _stub_rides["i2"] = _full_ride("i2", tss=40.0, dur_s=1800, z4=600)
    app_module._apply_rematch_preview_to_plan(plan, 0,
                                              _preview(monday.isoformat(), "i2"))
    second = s["execution"]
    assert second["activity_id"] == "i2"
    assert second["score"] != first["score"]
    assert second["computed_at"] >= first["computed_at"]


def test_fetch_failure_never_leaves_stale_score(_stub_rides):
    """If the new activity can't be fetched, a score computed from a
    DIFFERENT ride is dropped rather than left stale."""
    monday = date(2026, 6, 1)
    plan = _mk_week_plan(monday)
    _stub_rides["i1"] = _full_ride("i1")
    app_module._apply_rematch_preview_to_plan(plan, 0,
                                              _preview(monday.isoformat(), "i1"))
    s = plan["weeks"][0]["sessions"][0]
    assert s["execution"]["activity_id"] == "i1"
    # Rematch to i9 which is NOT in the archive.
    app_module._apply_rematch_preview_to_plan(plan, 0,
                                              _preview(monday.isoformat(), "i9"))
    assert s["execution"] is None


def test_fetch_helper_matches_all_id_forms(_stub_rides):
    _stub_rides["77"] = _full_ride("77")
    assert app_module._fetch_ride_for_execution("77")["ride_id"] == "icu_77"
    assert app_module._fetch_ride_for_execution("icu_77")["ride_id"] == "icu_77"
    assert app_module._fetch_ride_for_execution("nope") is None
    assert app_module._fetch_ride_for_execution(None) is None


# ── regression: score survives plan-reflow round-trips ──────────────────────

def _done_session_with_execution(monday: date) -> dict:
    return {"day": monday.isoformat(), "day_name": "Mon",
            "session_type": "threshold", "duration_min": 60,
            "tss_estimate": 80, "description": "", "zwo_file": "x.zwo",
            "zwo_name": "X", "status": "done",
            "completion_matches": [{"activity_id": "i1", "matched_axes": 3,
                                    "score": 0.95, "axes": {}, "details": None,
                                    "applied_at": "2026-06-01T10:00:00"}],
            "execution": {"score": 92, "basis": "power", "verdict": "on_target",
                          "components": {}, "activity_id": "i1",
                          "computed_at": "2026-06-01T10:00:00"}}


def test_execution_survives_reforecast_dict():
    """The reshuffle/reforecast write-back is diff-field-based — it must not
    strip the persisted execution key from a done session."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    plan = _mk_week_plan(monday)
    plan["weeks"][0]["sessions"][0] = _done_session_with_execution(monday)
    plan["goal"] = {"type": "general", "hours_per_week": 8}
    new_plan, _n, _info = tp.reforecast_dict(plan, today_iso=today.isoformat())
    s0 = new_plan["weeks"][0]["sessions"][0]
    assert s0["execution"]["score"] == 92


def test_execution_survives_refit_remaining_week():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sessions = [app_module._planned_session_from_json(
        _done_session_with_execution(monday))]
    for off in range(1, 7):
        sessions.append(tp.PlannedSession(
            day=monday + timedelta(days=off), day_name="D",
            session_type="z2" if off in (2, 4) else "rest",
            duration_min=60 if off in (2, 4) else 0,
            tss_estimate=45 if off in (2, 4) else 0, description=""))
    week = tp.PlannedWeek(week_num=1, start=monday,
                          end=monday + timedelta(days=6), phase="build1",
                          tss_target=300, is_stepback=False,
                          sessions=sessions)
    goal = tp.Goal(goal_type="general",
                   target_date=today + timedelta(weeks=8))
    weeks, _info = tp.refit_remaining_week(goal, [week], today)
    done = next(s for w in weeks for s in w.sessions
                if s.day == monday)
    assert getattr(done, "execution", None) == \
        _done_session_with_execution(monday)["execution"]


def test_execution_survives_regenerate_plan_dict(monkeypatch):
    """The full-regen core round-trips ALL dataclass fields — the past done
    session keeps its execution block."""
    today = date.today()
    monday = today - timedelta(days=today.weekday()) - timedelta(days=7)
    plan = _mk_week_plan(monday)  # last week, fully in the past
    plan["weeks"][0]["sessions"][0] = _done_session_with_execution(monday)
    # A current week so regenerate has something ahead of today.
    cur_monday = monday + timedelta(days=7)
    wk2 = _mk_week_plan(cur_monday)["weeks"][0]
    wk2["week_num"] = 2
    plan["weeks"].append(wk2)
    plan["goal"] = {"type": "general", "hours_per_week": 6,
                    "event_date": (today + timedelta(weeks=6)).isoformat()}
    new_plan, _info = app_module._regenerate_plan_dict(
        plan, current_ctl=50.0, activities=[], seed_salt=1)
    past = [s for w in new_plan["weeks"] for s in w["sessions"]
            if s.get("day") == monday.isoformat()]
    assert past, "past session disappeared from regenerated plan"
    assert past[0].get("execution", {}).get("score") == 92


# ── UI + payload structural checks ───────────────────────────────────────────

def test_calendar_planned_payload_carries_execution():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    i = src.find('"execution": (sess.get("execution")')
    assert i != -1, "calendar planned_payload must expose session execution"


def test_dashboard_renders_badge_and_breakdown():
    html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert "planned.execution" in html          # week-strip badge source
    assert "✓ ${Math.round(execBlock.score)}" in html  # ✓ NN badge
    assert "Execution <b" in html               # day-modal breakdown line
