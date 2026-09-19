"""Answers to the third adversarial review of the week-view programme
(2026-09-14): each test failed on 34072406 or on the mutant the reviewer
showed no test caught."""
from __future__ import annotations

import json
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import clock
import training_planner as tp


def _week_plan(today):
    mon = today - timedelta(days=today.weekday())
    sess = [{"day": (mon + timedelta(days=i)).isoformat(), "session_type": "z2", "duration_min": 60,
             "tss_estimate": 45, "zwo_file": "", "zwo_name": "", "status": "pending"} for i in range(7)]
    return {"goal": {"type": "general", "hours_per_week": 8.0, "rest_days": []}, "phases": [],
            "weeks": [{"week_num": 1, "start": mon.isoformat(), "end": (mon + timedelta(days=6)).isoformat(),
                       "phase": "base", "tss_target": 300, "is_stepback": False, "sessions": sess}]}


def test_a_ride_the_lazy_sync_fetched_adapts_on_the_next_post():
    """S1. The lazy sync a read kicks off stores a ride but does not adapt;
    the Home POST that follows finds the sync throttled and reports none
    added. On a day already adapted, the ride waited until tomorrow."""
    import ride_storage
    today = clock.today()
    tmp = Path(tempfile.mkdtemp())
    # The ride it syncs lands in a store of its own, not the worker's sandbox
    # archive, where later tests would find it.
    (tmp / "rides" / "icu").mkdir(parents=True)
    store = [patch.object(ride_storage, "_icu_rides_dir", return_value=tmp / "rides" / "icu"),
             patch.object(ride_storage, "_fit_rides_dir", return_value=tmp / "rides"),
             patch.object(app_module, "_rides_fit_dir", return_value=tmp / "rides")]
    for p in store:
        p.start()
    app_module.clear_cache()
    try:
        _lazy_ride_adapts(today, tmp)
    finally:
        for p in store:
            p.stop()
        app_module.clear_cache()


def _lazy_ride_adapts(today, tmp):
    plan = _week_plan(today)
    plan["reconcile_date"] = today.isoformat()
    plan["adapted_ride_total"] = len(app_module._load_all_rides_safe())
    assert plan["adapted_ride_total"] == 0
    (tmp / "current_plan.json").write_text(json.dumps(plan))
    state = {"last": time.time() - 7200}
    calls = []

    def spy(plan, **k):
        calls.append(1)
        return plan, "reforecasted", {}, {}
    act = [{"id": "i777000001", "type": "Ride", "start_date_local": f"{today.isoformat()}T07:00:00",
            "name": "new", "moving_time": 3600, "icu_training_load": 80}]
    with patch.object(app_module, "_plan_dir", return_value=tmp), \
         patch.object(app_module, "_icu_credentials_present", return_value=True), \
         patch.object(app_module, "_read_last_sync_at", side_effect=lambda: state["last"]), \
         patch.object(app_module, "_write_last_sync_at", side_effect=lambda t: state.__setitem__("last", t)), \
         patch.object(app_module, "_read_last_wellness_sync_at", return_value=time.time()), \
         patch.object(app_module, "_augment_icu_record_with_dfa", return_value=None), \
         patch.object(app_module, "_apply_plan_update", side_effect=spy), \
         patch("training.fetch_recent_activities", return_value=act):
        app_module._maybe_lazy_icu_sync(force_if_today_missing=True)
        assert calls == [], "a read's sync must not adapt"
        r = TestClient(app_module.app).post("/api/rides/sync").json()
    assert r.get("added") == 0          # the POST itself fetched nothing new
    assert calls == [1]
    assert r["plan_adapted"] == "reforecasted"


def test_a_stamp_only_write_is_not_an_adaptation():
    """Nit: a debounced reforecast writes only the daily stamp; the cards
    have nothing to repaint."""
    today = clock.today()
    tmp = Path(tempfile.mkdtemp())
    (tmp / "current_plan.json").write_text(json.dumps(_week_plan(today)))
    with patch.object(app_module, "_plan_dir", return_value=tmp), \
         patch.object(app_module, "_apply_plan_update",
                      side_effect=lambda plan, **k: (plan, "skipped", {}, {})):
        assert app_module._maybe_auto_reforecast("default", 0) is None
    assert json.loads((tmp / "current_plan.json").read_text())["reconcile_date"] == today.isoformat()


def _row(start, end, phase, tgt, stepback, carried=False):
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    sess = [{"day": (s + timedelta(days=i)).isoformat(), "session_type": "z2", "duration_min": 60,
             "tss_estimate": 50, "zwo_file": "endurance_steady_74pct_70min.zwo", "zwo_name": "E",
             "status": "pending"} for i in range((e - s).days + 1)]
    r = {"week_num": 1, "start": start, "end": end, "phase": phase, "tss_target": tgt,
         "is_stepback": stepback, "sessions": sess}
    if carried:
        r["carried_from_generate"] = "2026-09-15"
    return r


def test_a_carried_row_beats_a_history_shell():
    """S3. A replaced plan's Thursday-start row shares its ISO week with a
    Monday..Wednesday history shell; the dedupe ranked the shell first, so the
    calendar said "history" where the week view said the stored row."""
    clock.freeze(date(2026, 9, 15))
    try:
        plan = {"goal": {"type": "weeks", "hours_per_week": 8}, "phases": [],
                "replaced_weeks": [_row("2026-09-03", "2026-09-09", "oldbuild", 333, True, True),
                                   _row("2026-09-10", "2026-09-14", "oldbuild", 300, False, True)],
                "weeks": [_row("2026-09-15", "2026-09-20", "base", 250, False),
                          _row("2026-09-21", "2026-09-27", "base", 400, False)]}
        cal = app_module.merge_plan_with_rides(plan, [])
        wk = next(w for w in cal["weeks"] if w["start_date"] == "2026-08-31")
        view = app_module._stored_week_view(plan, -2).as_weekly_plan()
        assert (wk["phase"], wk["is_stepback"]) == (view["phase"], view["is_stepback"]) == ("oldbuild", True)
    finally:
        clock.unfreeze()


def test_the_sync_loop_applies_the_eftp_only_when_opted_in():
    """T1. The eFTP auto-apply moved to the 30-min sync loop's callback, and
    nothing tested it there: the loop registers _after_background_sync, which
    applies when the rider opted in and does not even fetch otherwise."""
    with TestClient(app_module.app):
        assert app_module.db.post_sync_callback is app_module._after_background_sync
    from profile_manager import ProfileManager
    pm = ProfileManager.get()
    for opted, expect in ((True, 1), (False, 0)):
        applied, fetched = [], []
        with patch.object(app_module, "_icu_push_daily_from_sync"), \
             patch.dict(pm.prefs, {"eftp_auto_apply": opted}), \
             patch.object(app_module, "fetch_wellness", side_effect=lambda n: fetched.append(n) or []), \
             patch.object(app_module, "_guarded_check_and_auto_apply_eftp",
                          side_effect=lambda w: applied.append(w)):
            app_module._after_background_sync()
        assert (len(applied), len(fetched)) == (expect, expect)


def test_the_calendar_cell_and_today_carry_the_riders_marks():
    """T2. The skip fix's server halves: the calendar cell's status and move
    mark (the strip and the day modal read them) and today's zwo_name."""
    today = clock.today()
    tmp = Path(tempfile.mkdtemp())
    plan = _week_plan(today)
    for s in plan["weeks"][0]["sessions"]:
        s.update(zwo_file="endurance_steady_74pct_70min.zwo", zwo_name="Endurance Steady (70min)")
        if s["day"] == today.isoformat():
            s.update(status="dismissed", user_moved=True)
    (tmp / "current_plan.json").write_text(json.dumps(plan))
    with patch.object(app_module, "_plan_dir", return_value=tmp), \
         patch.object(app_module, "_kick_lazy_icu_sync", return_value=False):
        c = TestClient(app_module.app)
        cal = c.get("/api/calendar").json()
        cell = next(d for w in cal["weeks"] for d in w["days"] if d["date"] == today.isoformat())["planned"]
        assert (cell["status"], cell["user_moved"]) == ("dismissed", True)
        assert c.get("/api/today-session").json()["planned"]["zwo_name"] == "Endurance Steady (70min)"
    src = (Path(app_module.__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")
    synth = src[src.index("  const synth = {"):]
    synth = synth[:synth.index("};")]
    assert "status: p.status || 'pending'" in synth
