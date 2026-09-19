"""v3.11.5 — persisted plans with file-less sessions heal themselves.

A Linux rider's grid read "Z2 — no workout matched" on Mon/Tue/Wed/Thu/Sun for
weeks. Under 3.11.1 the availability reflow re-matched those slots against an
EMPTY library (custom workouts folder existed, held nothing) and blanked them;
3.11.2 fixed the library, but nothing ever re-matched a blank session (R4a
skips file-less slots; the generation sweep runs only at generation).

Pinned here:
  1. the reflow never blanks a session that had a file (keeps it, narrates);
  2. heal_unmatched_sessions_dict gives upcoming pending blank sessions a file
     and leaves past / dismissed / race / opener / ridden ones alone;
  3. the heal runs at startup and after a plan-writing POST, once per
     plan-file version, and persists; a GET never writes (P8, v3.12.0);
  4. /api/diag/health reports the count.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

import training_planner as tp


@pytest.fixture(scope="module")
def library():
    lib = tp.load_workout_library()
    assert len(lib) >= 4000
    return lib


def _next_monday() -> date:
    t = date.today()
    d = (7 - t.weekday()) % 7 or 7
    return t + timedelta(days=d)


def _mk(day, st, dur, tss):
    return tp.PlannedSession(day=day, day_name=day.strftime("%A"), session_type=st,
                             duration_min=dur, tss_estimate=tss, description="")


def _matched_week(mon, library):
    spec = [("z2", 90, 68), ("z2", 90, 68), ("z2", 90, 68), ("z2", 90, 68),
            ("sweetspot", 60, 80), ("z2", 90, 68), ("z2", 180, 135)]
    sessions = [_mk(mon + timedelta(days=i), st, d, t) for i, (st, d, t) in enumerate(spec)]
    wk = tp.PlannedWeek(week_num=1, start=mon, end=mon + timedelta(days=6), phase="build1",
                        tss_target=550.0, is_stepback=False, sessions=sessions, hit_per_week=1)
    used = set()
    for i, s in enumerate(sessions):
        tp.match_zwo(s, library, week_num=1, day_idx=i, used_names=used)
        used.add(s.zwo_name)
    assert all(s.zwo_file for s in sessions)
    return wk


# The rider's availability, from the screenshot: Mon 1h Tue 2h Wed 1h Thu 2h Fri 1h Sat 1.5h Sun 4h.
_RIDER_HOURS = [1.0, 2.0, 1.0, 2.0, 1.0, 1.5, 4.0]


def test_availability_reflow_keeps_the_file_when_the_library_is_empty(monkeypatch, library):
    mon = _next_monday()
    wk = _matched_week(mon, library)
    before = {s.day: s.zwo_file for s in wk.sessions}
    monkeypatch.setattr(tp, "load_workout_library", lambda *a, **k: [])
    overrides = {(mon + timedelta(days=i)).isoformat(): h for i, h in enumerate(_RIDER_HOURS)}
    tsb = {mon + timedelta(days=i): 0.0 for i in range(7)}
    tp.reforecast(tp.Goal(goal_type="general", hours_per_week=12.5), [wk],
                  tsb_series=tsb, availability_overrides=overrides)
    # D5 (v3.12.0): free hours are a ceiling, never a prescription. A day with
    # less time than its session shrinks to it (Mon 1 h, Wed 1 h); the minutes
    # cut are redistributed within the OTHER days' ceilings so the week's
    # load is conserved -- never inflated to fill every free hour (the old
    # scaler turned a 230 TSS week into 496).
    for i, s in enumerate(wk.sessions):
        assert s.duration_min <= _RIDER_HOURS[i] * 60 + 1, (s.day, s.duration_min, _RIDER_HOURS[i])
    by_day = {s.day: s for s in wk.sessions}
    assert by_day[mon].duration_min == 60 and by_day[mon + timedelta(days=2)].duration_min == 60
    z2_before = 90 * 5 + 180
    z2_after = sum(s.duration_min for s in wk.sessions if s.session_type == "z2")
    assert abs(z2_after - z2_before) <= 15, (z2_before, z2_after)      # conserved, not scaled to 12.5 h
    assert all(s.zwo_file == before[s.day] for s in wk.sessions), \
        "a session that had a file must keep it when no candidate exists"


def test_availability_reflow_with_a_real_library_rematches_instead(library):
    mon = _next_monday()
    wk = _matched_week(mon, library)
    overrides = {(mon + timedelta(days=i)).isoformat(): h for i, h in enumerate(_RIDER_HOURS)}
    tsb = {mon + timedelta(days=i): 0.0 for i in range(7)}
    tp.reforecast(tp.Goal(goal_type="general", hours_per_week=12.5), [wk],
                  tsb_series=tsb, availability_overrides=overrides)
    assert all(s.zwo_file for s in wk.sessions)


def _plan_dict(mon, *, blank_days=(0, 1, 2, 3, 6)):
    """Persisted-plan shape with the rider's damage: blank files on the given days."""
    spec = [("z2", 60, 45), ("z2", 120, 90), ("z2", 60, 45), ("z2", 120, 90),
            ("sweetspot", 60, 80), ("z2", 90, 68), ("z2", 240, 180)]
    sessions = []
    for i, (st, d, t) in enumerate(spec):
        day = mon + timedelta(days=i)
        sessions.append({"day": day.isoformat(), "day_name": day.strftime("%A"), "session_type": st,
                         "duration_min": d, "tss_estimate": t, "description": f"{st} ({d}min) — availability adjusted",
                         "zwo_file": "" if i in blank_days else f"keep_{i}.zwo",
                         "zwo_name": "" if i in blank_days else f"Keep {i}", "status": "pending"})
    return {"goal": {"type": "general", "hours_per_week": 12.5}, "phases": [],
            "weeks": [{"week_num": 1, "start": mon.isoformat(), "end": (mon + timedelta(days=6)).isoformat(),
                       "phase": "build1", "tss_target": 550, "is_stepback": False, "sessions": sessions}],
            "generated": "2026-09-01T00:00:00"}


def test_heal_gives_upcoming_blank_sessions_a_file_and_leaves_the_rest_alone(library):
    mon = _next_monday()
    plan = _plan_dict(mon)
    # untouchables: a past blank, a dismissed blank, a race, an opener, a ridden one
    past = (date.today() - timedelta(days=3)).isoformat()
    plan["weeks"][0]["sessions"] += [
        {"day": past, "session_type": "z2", "duration_min": 60, "tss_estimate": 45, "zwo_file": "", "status": "pending"},
        {"day": (mon + timedelta(days=7)).isoformat(), "session_type": "z2", "duration_min": 60, "tss_estimate": 45, "zwo_file": "", "status": "pending", "dismissed_at": "2026-09-01T00:00:00"},
        {"day": (mon + timedelta(days=8)).isoformat(), "session_type": "z2", "duration_min": 60, "tss_estimate": 45, "zwo_file": "", "status": "pending", "is_race": True},
        {"day": (mon + timedelta(days=9)).isoformat(), "session_type": "z2", "duration_min": 30, "tss_estimate": 20, "zwo_file": "", "status": "pending", "is_opener": True},
        {"day": (mon + timedelta(days=10)).isoformat(), "session_type": "z2", "duration_min": 60, "tss_estimate": 45, "zwo_file": "", "status": "done"},
        {"day": (mon + timedelta(days=11)).isoformat(), "session_type": "z2", "duration_min": 0, "tss_estimate": 0, "zwo_file": "", "status": "pending"},
    ]
    assert tp.count_unmatched_pending_sessions(plan, today=date.today()) == 5
    stats = tp.heal_unmatched_sessions_dict(plan, library, today=date.today())
    assert stats == {"candidates": 5, "healed": 5, "still_unmatched": 0}
    sessions = plan["weeks"][0]["sessions"]
    healed = sessions[:7]
    assert all(s["zwo_file"] for s in healed)
    assert all(s.get("matched") is True for i, s in enumerate(healed) if i in (0, 1, 2, 3, 6))
    assert [s["zwo_file"] for i, s in enumerate(healed) if i in (4, 5)] == ["keep_4.zwo", "keep_5.zwo"]
    assert len({s["zwo_name"] for s in healed}) == 7          # plan-wide uniqueness respected
    assert [s["duration_min"] for s in healed] == [60, 120, 60, 120, 60, 90, 240]   # slots untouched
    assert all(s["zwo_file"] == "" for s in sessions[7:])   # past / dismissed / race / opener / done / zero-length untouched
    assert tp.count_unmatched_pending_sessions(plan, today=date.today()) == 0


def test_heal_skips_a_malformed_session_and_heals_the_rest(library):
    plan = _plan_dict(_next_monday())
    plan["weeks"][0]["sessions"][1]["duration_min"] = "not-a-number"      # would raise in int()
    stats = tp.heal_unmatched_sessions_dict(plan, library, today=date.today())
    assert stats == {"candidates": 5, "healed": 4, "still_unmatched": 1}
    assert plan["weeks"][0]["sessions"][1]["zwo_file"] == ""
    assert all(plan["weeks"][0]["sessions"][i]["zwo_file"] for i in (0, 2, 3, 6))


def test_heal_with_an_empty_library_changes_nothing(library):
    plan = _plan_dict(_next_monday())
    assert tp.heal_unmatched_sessions_dict(plan, [], today=date.today()) == {"candidates": 0, "healed": 0, "still_unmatched": 0}
    assert tp.count_unmatched_pending_sessions(plan) == 5


def test_diag_health_counts_sessions_without_file(monkeypatch, tmp_path):
    import app as app_module
    from fastapi.testclient import TestClient
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    (tmp_path / "current_plan.json").write_text(json.dumps(_plan_dict(_next_monday())))
    app_module._DIAG_HEALTH_CACHE["result"] = None
    app_module._DIAG_HEALTH_CACHE["ts"] = 0.0
    r = TestClient(app_module.app).get("/api/diag/health")
    assert r.status_code == 200
    assert r.json()["checks"]["plan_readable"]["sessions_without_file"] == 5


def test_heal_passes_the_hr_mode_bias_through(monkeypatch, library):
    seen = {}
    real = tp.match_zwo
    def spy(session, lib, **kw):
        seen["hr_bias"] = kw.get("hr_bias"); return real(session, lib, **kw)
    monkeypatch.setattr(tp, "match_zwo", spy)
    tp.heal_unmatched_sessions_dict(_plan_dict(_next_monday()), library, today=date.today(), hr_bias=True)
    assert seen["hr_bias"] is True


# ── P8 (v3.12.0): reads do not write ──────────────────────────────────────────

def test_get_api_plan_does_not_heal_or_write(monkeypatch, tmp_path, library):
    """A GET serves the plan as it is on disk and changes nothing there."""
    import app as app_module
    from fastapi.testclient import TestClient
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    json_path = tmp_path / "current_plan.json"
    json_path.write_text(json.dumps(_plan_dict(_next_monday())))
    before = json_path.stat().st_mtime_ns
    app_module._PLAN_HEAL_SEEN.clear()
    calls = []
    monkeypatch.setattr(tp, "heal_unmatched_sessions_dict", lambda *a, **k: (calls.append(1), {"candidates": 5, "healed": 5, "still_unmatched": 0})[1])
    r = TestClient(app_module.app).get("/api/plan")
    assert r.status_code == 200
    assert calls == [], "a GET must not run the heal"
    assert json_path.stat().st_mtime_ns == before
    assert not (tmp_path / "current_plan.json.bak").exists()
    assert json.loads(json_path.read_text())["weeks"][0]["sessions"][0]["zwo_file"] == ""   # disk untouched


def test_startup_heal_heals_and_persists_with_a_backup(monkeypatch, tmp_path, library):
    import app as app_module
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    json_path = tmp_path / "current_plan.json"
    json_path.write_text(json.dumps(_plan_dict(_next_monday())))
    app_module._PLAN_HEAL_SEEN.clear()
    app_module._heal_plan_on_disk()                                  # what lifespan calls
    on_disk = json.loads(json_path.read_text())["weeks"][0]["sessions"][:7]
    assert all(s["zwo_file"] for s in on_disk), [s.get("zwo_file") for s in on_disk]
    assert (tmp_path / "current_plan.json.bak").exists()
    assert json.loads((tmp_path / "current_plan.json.bak").read_text())["weeks"][0]["sessions"][0]["zwo_file"] == ""


def test_heal_runs_once_per_file_version(monkeypatch, tmp_path):
    """The mtime cache, proven independently of content: a heal that fixes
    nothing (still damaged file) must not be re-run until the file changes."""
    import app as app_module
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    json_path = tmp_path / "current_plan.json"
    json_path.write_text(json.dumps(_plan_dict(_next_monday())))
    app_module._PLAN_HEAL_SEEN.clear()
    calls = []
    monkeypatch.setattr(tp, "heal_unmatched_sessions_dict",
                        lambda *a, **k: (calls.append(1), {"candidates": 5, "healed": 0, "still_unmatched": 5})[1])
    app_module._heal_plan_on_disk(); app_module._heal_plan_on_disk(); app_module._heal_plan_on_disk()
    assert calls == [1], "same file version must not be healed again"
    json_path.write_text(json.dumps(_plan_dict(_next_monday())))   # rewrite = new mtime = new version
    import os, time
    os.utime(json_path, ns=(time.time_ns(), time.time_ns()))
    app_module._heal_plan_on_disk()
    assert calls == [1, 1]


def test_the_heal_follows_a_plan_write_not_a_read_and_not_a_preview(monkeypatch, tmp_path):
    """The middleware: a GET never heals; a POST under /api/plan heals only
    when it changed the plan file (a preview that writes nothing heals
    nothing, so "apply=0 does not write" stays true); a POST under
    /api/profiles (a switch) heals the newly active plan."""
    import app as app_module
    from fastapi.testclient import TestClient
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    json_path = tmp_path / "current_plan.json"
    json_path.write_text(json.dumps(_plan_dict(_next_monday())))
    app_module._PLAN_HEAL_SEEN.clear()
    calls = []
    monkeypatch.setattr(tp, "heal_unmatched_sessions_dict",
                        lambda *a, **k: (calls.append(1), {"candidates": 5, "healed": 0, "still_unmatched": 5})[1])
    client = TestClient(app_module.app)
    client.get("/api/plan"); client.get("/api/calendar"); client.get("/api/diag/health")
    assert calls == [], "a GET never heals"
    r = client.post("/api/plan/_p8_probe")                          # a POST that wrote nothing
    assert r.status_code in (404, 405) and calls == []
    r = client.post("/api/plan/rematch?apply=0")                    # a preview: no write, no heal
    assert r.status_code == 200 and calls == []
    r = client.post("/api/profiles/_p8_probe")                      # a switch heals the active plan
    assert r.status_code in (404, 405) and calls == [1]
    import os, time
    json_path.write_text(json.dumps(_plan_dict(_next_monday())))    # simulate a POST that wrote the plan
    os.utime(json_path, ns=(time.time_ns(), time.time_ns()))
    app_module._heal_plan_on_disk()
    assert calls == [1, 1]


def test_heal_never_overwrites_a_plan_written_in_between(monkeypatch, tmp_path, library):
    """Lost-update guard: the heal read the file outside the lock; a regenerate
    that lands before the heal takes the lock must win, and must be what the
    heal operates on."""
    import app as app_module
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    json_path = tmp_path / "current_plan.json"
    json_path.write_text(json.dumps(_plan_dict(_next_monday())))
    app_module._PLAN_HEAL_SEEN.clear()
    fresh_plan = _plan_dict(_next_monday(), blank_days=())        # a concurrent regenerate: all matched
    fresh_plan["marker"] = "written-by-regenerate"
    real_count = tp.count_unmatched_pending_sessions

    def count_then_swap(plan_dict, today=None):
        n = real_count(plan_dict, today)                           # pre-check on the outside-lock copy (5 blanks)
        json_path.write_text(json.dumps(fresh_plan))               # ...and meanwhile someone else wrote the file
        return n
    monkeypatch.setattr(tp, "count_unmatched_pending_sessions", count_then_swap)
    app_module._heal_plan_on_disk()
    on_disk = json.loads(json_path.read_text())
    assert on_disk.get("marker") == "written-by-regenerate"        # the concurrent write survived
    assert [s["zwo_file"] for s in on_disk["weeks"][0]["sessions"]] == [f"keep_{i}.zwo" for i in range(7)]
