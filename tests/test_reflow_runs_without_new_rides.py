"""A miss is the ABSENCE of a ride — the adaptation chain must still run.

The whole reconcile -> auto-reschedule -> refit chain hung off
``_maybe_auto_reforecast(profile_id, new_rides)``, which returned immediately
when ``new_rides <= 0``. So the one event the chain most needs to react to —
a rider who stopped riding — was the one event that skipped it. Sessions were
never marked "missed", ``refit_remaining_week`` was handed an empty missed
list, and the week kept the shape it was born with. The rider's own plan had
a fully-past week with all seven sessions still ``status="pending"``.

Three regressions are pinned here, all reachable from that one report:

  1. the daily pass runs with zero new rides, and only once per day;
  2. a reforecast debounce ("skipped") no longer discards the statuses the
     reconcile pass just wrote;
  3. a reforecast that changed nothing stops claiming it rebalanced.

The hard-only refit gate is NOT pinned here — it is deliberate and already
covered by tests/test_v207_missed_hard_refit.py. Missed easy volume is written
off on purpose: no study has ever randomised athletes to redistribute-vs-let-
it-go, and the adjacent evidence (Hickson's reduced-training series, Bosquet's
taper meta-analysis) says one short week costs essentially nothing as long as
intensity is preserved.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

import app as app_mod


@pytest.fixture()
def plan_on_disk(tmp_path, monkeypatch):
    """A plan whose current week is half missed, wired to a temp plan dir."""
    monday = date(2026, 8, 3)
    weeks = [{
        "week_num": 1,
        "start": monday.isoformat(),
        "end": (monday + timedelta(days=6)).isoformat(),
        "phase": "build1",
        "tss_target": 300,
        "sessions": [
            {"day": (monday + timedelta(days=i)).isoformat(),
             "day_name": (monday + timedelta(days=i)).strftime("%A"),
             "description": "Endurance ride",
             "session_type": "z2", "duration_min": 60, "tss_estimate": 45,
             "status": "pending", "zwo_file": "endurance_steady_65pct_60min.zwo"}
            for i in range(7)
        ],
    }]
    plan = {"goal": {"distribution": "polarized"}, "phases": [], "weeks": weeks}
    d = tmp_path / "plans"
    d.mkdir(parents=True)
    (d / "current_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(app_mod, "_plan_dir", lambda: d)
    monkeypatch.setattr(app_mod.db, "query_activities", lambda **kw: [])
    monkeypatch.setattr(app_mod, "cached", lambda *a, **k: {"ctl": 40, "tsb": 5})
    return d / "current_plan.json"


def _run(monkeypatch, new_rides, today=date(2026, 8, 6)):
    """Invoke the sync hook with the clock pinned to mid-week Thursday."""
    class _D(date):
        @classmethod
        def today(cls):
            return today
    monkeypatch.setattr(app_mod, "date", _D)
    app_mod._maybe_auto_reforecast("default", new_rides)


def test_chain_runs_when_no_new_rides_arrived(plan_on_disk, monkeypatch):
    """The regression itself: zero new rides must not short-circuit."""
    seen = {}

    def _spy(plan, today):
        seen["ran"] = True
        return 0, None

    monkeypatch.setattr(app_mod, "_reconcile_current_week", _spy)
    _run(monkeypatch, new_rides=0)
    assert seen.get("ran"), (
        "reconcile never ran with new_rides=0 — a rider who stops riding is "
        "exactly the rider whose sessions never get marked missed")


def test_daily_pass_runs_once_per_day(plan_on_disk, monkeypatch):
    """It is a DAILY pass, not a per-sync one: the stamp must bind."""
    calls = []
    monkeypatch.setattr(app_mod, "_reconcile_current_week",
                        lambda plan, today: (calls.append(today), (0, None))[1])
    _run(monkeypatch, new_rides=0)
    _run(monkeypatch, new_rides=0)
    _run(monkeypatch, new_rides=0)
    assert len(calls) == 1, f"daily pass ran {len(calls)}x in one day"
    stamp = json.loads(plan_on_disk.read_text())["reconcile_date"]
    assert stamp == "2026-08-06"


def test_new_rides_still_bypass_the_daily_stamp(plan_on_disk, monkeypatch):
    """An arriving ride must adapt immediately, stamp or no stamp."""
    calls = []
    monkeypatch.setattr(app_mod, "_reconcile_current_week",
                        lambda plan, today: (calls.append(today), (0, None))[1])
    _run(monkeypatch, new_rides=0)
    _run(monkeypatch, new_rides=2)
    assert len(calls) == 2, "a newly-synced ride was swallowed by the stamp"


def test_debounced_reforecast_keeps_the_missed_marks(plan_on_disk, monkeypatch):
    """A "skipped" reforecast must not discard reconcile's work.

    The reforecast tier debounces at 5 minutes. Its caller treated "skipped"
    as nothing-to-write, so syncing twice inside that window threw away the
    statuses reconcile had just marked — and the plan ended the day still
    calling every past session pending.
    """
    plan = json.loads(plan_on_disk.read_text())
    plan["reforecast_date"] = datetime.now().isoformat()   # inside the debounce
    plan_on_disk.write_text(json.dumps(plan), encoding="utf-8")

    def _marks_two(plan, today):
        for s in plan["weeks"][0]["sessions"][:2]:
            s["status"] = "missed"
        return 2, None

    monkeypatch.setattr(app_mod, "_reconcile_current_week", _marks_two)
    monkeypatch.setattr(app_mod, "_auto_apply_missed_moves", lambda p, t: [])
    _run(monkeypatch, new_rides=1)

    after = json.loads(plan_on_disk.read_text())
    statuses = [s["status"] for s in after["weeks"][0]["sessions"][:2]]
    assert statuses == ["missed", "missed"], (
        f"reconcile's marks were discarded by the reforecast debounce: {statuses}")


def test_unchanged_plan_does_not_claim_it_rebalanced(plan_on_disk, monkeypatch):
    """Every reforecast mutator is downward-only and none read status=="missed",
    so an absence lands there and changes nothing — while telling the rider the
    plan was "rebalanced to today's fitness"."""
    monkeypatch.setattr(app_mod, "_reconcile_current_week", lambda p, t: (0, None))
    monkeypatch.setattr(app_mod, "_auto_apply_missed_moves", lambda p, t: [])
    monkeypatch.setattr(app_mod.tp, "reforecast_dict",
                        lambda plan, **kw: (plan, False, {}))

    plan = json.loads(plan_on_disk.read_text())
    _out, action, _info, status = app_mod._apply_plan_update(
        plan, training={"ctl": 40, "tsb": 5}, activities=[],
        today=date(2026, 8, 6), allow_regen=False, gap_debounce=True)

    assert action == "rebalanced"
    assert "no change" in status.lower(), (
        f"unchanged plan still reported {status!r}")
