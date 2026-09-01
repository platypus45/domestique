"""Wave 1 E2 matrix + E8 opener-survival tests (v2.5.0, P4.1 audit).

E2 — the full 7-weekday-offset × {12,16,24}-week runway matrix over the FC1
clip engine + FC2a taper enforcement:
  * disjoint, contiguous week-row spans; union = [plan start, target]
  * every row inside exactly ONE phase; no session dated past the target
  * taper wk1 ≤ 0.65 × the ACTUAL pre-taper max (full non-stepback rows only —
    E11: stepback-before-taper rows are exempt from the reference)
  * race week (training only) < taper wk1
  * openers present (T-1 is_opener), race day marked

E8 — the 4-step opener-survival chain: generate → reforecast_dict → refit →
regenerate_from_today. The opener must be present (and opener-flagged) after
every step; this is what E7's round-trip fields + the _demote_hit_window /
_enforce_weekly_hit_cap whitelists exist for.

Pinned env (W8 pattern): current_ctl=50.0, recent_weekly_tss=650.0, frozen
today = 2026-01-05 (Monday), seed_salt=0. This module uses its OWN mutable
frozen-date class (not conftest's) because E8's refit/regen steps need to
advance "today" mid-chain.
"""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

import training_planner as tp
from conftest import (
    PLANNER_PIN_ANCHOR as ANCHOR,
    PLANNER_PIN_ARGS,
)

_LIB_INDEX = Path(__file__).resolve().parent.parent / "src" / "workouts" / ".library_index.json"


class _FrozenDate(date):
    _today = ANCHOR

    @classmethod
    def today(cls):
        return cls(cls._today.year, cls._today.month, cls._today.day)


@pytest.fixture(scope="module", autouse=True)
def _pinned_env():
    backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tp, "date", _FrozenDate)
        mp.setattr(tp, "get_today_metrics", lambda: {})
        yield
    _FrozenDate._today = ANCHOR
    if backup is not None and _LIB_INDEX.read_bytes() != backup:
        _LIB_INDEX.write_bytes(backup)


@pytest.fixture(autouse=True)
def _reset_today():
    _FrozenDate._today = ANCHOR
    yield
    _FrozenDate._today = ANCHOR


def _event_goal(target):
    return tp.Goal(goal_type="event", target_date=target,
                   event_name="TestFondo", event_km=150.0,
                   event_climb_m=1500.0)


def _wtss(w, include_race=True):
    return round(sum((s.tss_estimate or 0) for s in w.sessions
                     if s.session_type != "rest"
                     and (include_race or not getattr(s, "is_race", False))))


# ── E2 matrix (parallel build + vectorized asserts) ─────────────────────────
#
# The 21 scenario plans are independent, so they are built ONCE in a process
# pool (each worker self-pins the frozen date + metrics stub — monkeypatches
# don't cross process boundaries) and the parametrized tests assert against
# the precomputed summaries. Serial: ~20 s; pooled: wall ≈ slowest build +
# spawn overhead. Assertions run on numpy arrays (day ordinals, weekly TSS).

_MATRIX_CELLS = [(rw, off) for rw in (12, 16, 24) for off in range(7)]


def _e2_worker(cell):
    """Top-level (picklable) worker: pin the env in THIS process, build one
    plan, return a compact pickle-safe summary."""
    runway_weeks, offset = cell
    import training_planner as wtp
    from conftest import PLANNER_PIN_ANCHOR as A, PLANNER_PIN_ARGS as PIN

    class _WFrozen(date):
        @classmethod
        def today(cls):
            return cls(A.year, A.month, A.day)

    wtp.date = _WFrozen
    wtp.get_today_metrics = lambda: {}
    target = A + timedelta(days=runway_weeks * 7 + offset)
    phases, weeks = wtp.generate_plan(
        wtp.Goal(goal_type="event", target_date=target,
                 event_name="TestFondo", event_km=150.0,
                 event_climb_m=1500.0),
        seed_salt=0, **PIN)
    return {
        "target": target.toordinal(),
        "phases": [(p.name, p.start.toordinal(), p.end.toordinal()) for p in phases],
        "rows": [{
            "week_num": w.week_num, "phase": w.phase,
            "start": w.start.toordinal(), "end": w.end.toordinal(),
            "is_stepback": bool(w.is_stepback),
            "days": [s.day.toordinal() for s in w.sessions],
            "tss": [float(s.tss_estimate or 0) for s in w.sessions],
            "types": [s.session_type for s in w.sessions],
            "is_race": [bool(getattr(s, "is_race", False)) for s in w.sessions],
            "is_opener": [bool(getattr(s, "is_opener", False)) for s in w.sessions],
            "dur": [int(s.duration_min or 0) for s in w.sessions],
        } for w in weeks],
    }


@pytest.fixture(scope="module")
def matrix(_pinned_env):
    workers = min(7, max(2, (os.cpu_count() or 4) - 1))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_e2_worker, _MATRIX_CELLS))
    return dict(zip(_MATRIX_CELLS, results))


@pytest.mark.parametrize("offset", range(7))
@pytest.mark.parametrize("runway_weeks", [12, 16, 24])
def test_e2_matrix_clip_and_taper_invariants(matrix, runway_weeks, offset):
    m = matrix[(runway_weeks, offset)]
    target = m["target"]
    rows = sorted(m["rows"], key=lambda r: r["start"])

    # 1+4. Disjoint session days, all within row spans, none past target
    #      (vectorized over day ordinals).
    all_days = np.concatenate([np.asarray(r["days"], dtype=int) for r in rows]) \
        if any(r["days"] for r in rows) else np.array([], dtype=int)
    assert all_days.size == np.unique(all_days).size, "duplicate session days"
    assert int(all_days.max(initial=0)) <= target, "session past target"
    for r in rows:
        d = np.asarray(r["days"], dtype=int)
        if d.size:
            assert d.min() >= r["start"] and d.max() <= r["end"]

    # 2. Contiguous rows, union = [start, target].
    starts = np.array([r["start"] for r in rows])
    ends = np.array([r["end"] for r in rows])
    assert starts[0] == ANCHOR.toordinal()
    assert ends[-1] == target
    assert np.all(starts[1:] - ends[:-1] == 1), "row spans not contiguous"

    # 3. Every row inside exactly one phase.
    for r in rows:
        assert any(name == r["phase"] and ps <= r["start"] and r["end"] <= pe
                   for name, ps, pe in m["phases"]), (
            f"row {r['week_num']} ({r['phase']}) crosses a phase boundary")

    # 4b. Race day marked ON the target.
    assert any(f and d == target for r in rows
               for d, f in zip(r["days"], r["is_race"])), "race day unmarked"

    # 5. Taper budget (vectorized weekly TSS).
    def wtss(r, include_race=True):
        t = np.asarray(r["tss"]); ty = np.asarray(r["types"], dtype=object)
        rc = np.asarray(r["is_race"], dtype=bool)
        keep = ty != "rest"
        if not include_race:
            keep &= ~rc
        return float(t[keep].sum()) if t.size else 0.0

    tapers = [r for r in rows if r["phase"] == "taper"]
    assert len(tapers) == 2
    builds = [r for r in rows if r["phase"] != "taper" and not r["is_stepback"]
              and (r["end"] - r["start"]) + 1 >= 7]
    peak_ref = max(wtss(r) for r in builds[-3:])
    t1 = wtss(tapers[0])
    race_training = wtss(tapers[1], include_race=False)
    assert t1 <= 0.65 * peak_ref, (
        f"taper wk1 {t1} > 0.65×{peak_ref} (runway {runway_weeks}w offset {offset})")
    assert race_training < t1

    # 6. Openers at T-1, ≤50 min.
    eve = [(f, du) for r in rows
           for d, f, du in zip(r["days"], r["is_opener"], r["dur"])
           if target - d == 1]
    assert eve and eve[0][0], "T-1 opener missing"
    assert eve[0][1] <= 50


def _legacy_serial_build(runway_weeks, offset):
    target = ANCHOR + timedelta(days=runway_weeks * 7 + offset)
    phases, weeks = tp.generate_plan(_event_goal(target),
                                     seed_salt=0, **PLANNER_PIN_ARGS)
    return phases, weeks  # kept for local debugging


# ── E8 opener-survival chain ─────────────────────────────────────────────────

def _serialize(weeks, goal):
    """Mirror api_plan_generate's plan_dict serialization (incl. the E7 field
    set) so the chain exercises the REAL persisted shape."""
    return {
        "goal": {
            "type": goal.goal_type,
            "event_date": goal.target_date.isoformat(),
            "event_name": goal.event_name, "event_km": goal.event_km,
            "event_climb": goal.event_climb_m, "event_type": goal.event_type,
            "hours_per_week": goal.hours_per_week,
            "rest_days": goal.rest_days,
            "available_days": goal.available_days,
            "events": [],
        },
        "weeks": [{
            "week_num": w.week_num, "start": w.start.isoformat(),
            "end": w.end.isoformat(), "phase": w.phase,
            "tss_target": w.tss_target, "is_stepback": w.is_stepback,
            "sessions": [{
                "day": s.day.isoformat(), "day_name": s.day_name,
                "session_type": s.session_type,
                "duration_min": s.duration_min,
                "tss_estimate": s.tss_estimate,
                "description": s.description,
                "zwo_file": s.zwo_file, "zwo_name": s.zwo_name,
                "status": getattr(s, "status", "pending"),
                "user_moved": getattr(s, "user_moved", False),
                "dismissed_at": getattr(s, "dismissed_at", ""),
                "is_race": bool(getattr(s, "is_race", False)),
                "race": getattr(s, "race", None),
                "is_opener": bool(getattr(s, "is_opener", False)),
            } for s in w.sessions],
        } for w in weeks],
    }


def test_e8_opener_survives_generate_reforecast_refit_regen():
    target = ANCHOR + timedelta(days=112)
    t1_day = target - timedelta(days=1)
    goal = _event_goal(target)

    # STEP 1 — generate: opener born at T-1.
    _, weeks = tp.generate_plan(goal, seed_salt=0, **PLANNER_PIN_ARGS)
    op = next(s for w in weeks for s in w.sessions if s.day == t1_day)
    assert getattr(op, "is_opener", False), "step 1 (generate): opener missing"

    # STEP 2 — reforecast_dict on the persisted shape.
    pd = _serialize(weeks, goal)
    pd, _, _ = tp.reforecast_dict(pd, today_iso=ANCHOR.isoformat())
    op_j = next(s for w in pd["weeks"] for s in w["sessions"]
                if s["day"] == t1_day.isoformat())
    assert op_j["is_opener"] and op_j["duration_min"] <= 50, (
        f"step 2 (reforecast): opener lost/flattened: {op_j}")

    # STEP 3 — missed-hard refit mid-plan (today advanced into week 9).
    pw = tp._plan_dict_to_planned_weeks(pd)
    _FrozenDate._today = ANCHOR + timedelta(days=58)  # Wed of week 9
    today = _FrozenDate.today()
    cur = next(w for w in pw if w.start <= today <= w.end)
    missed = next((s for s in cur.sessions
                   if s.day < today and tp._session_is_hit(s)), None)
    if missed is None:  # force one so the refit tier actually fires
        missed = next(s for s in cur.sessions
                      if s.day < today and s.session_type != "rest")
        missed.session_type = "vo2max"
    missed.status = "missed"
    pw, refit_info = tp.refit_remaining_week(goal, pw, today)
    assert refit_info["action"] == "refitted"
    op3 = next(s for w in pw for s in w.sessions if s.day == t1_day)
    assert getattr(op3, "is_opener", False), "step 3 (refit): opener lost"

    # STEP 4 — regenerate_from_today rebuilds the future weeks.
    _, all_weeks, _ = tp.regenerate_from_today(goal, pw, current_ctl=55.0)
    op4 = next(s for w in all_weeks for s in w.sessions if s.day == t1_day)
    assert getattr(op4, "is_opener", False), "step 4 (regen): opener lost"
    assert not [s for w in all_weeks for s in w.sessions if s.day > target]
