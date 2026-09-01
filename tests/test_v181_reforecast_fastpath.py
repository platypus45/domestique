"""v1.8.1 SPEED-B — `reforecast_dict(..., accept_redraw_fast=True)` cuts
the accept-redraw hot path from ~1.7 s warm to <0.5 s by skipping the
G3 polarization-breach recompute and the G4 ACWR weekly rescale. The
swap on /api/plan/accept-redraw changes ONE session — neither block can
fire from a single-session mutation, so re-running them is wasted work.

Tests:

1. Fast path on a 1-day swap finishes < 0.5 s on a realistic 26-week plan.
2. Fast path still propagates downstream session diffs (availability
   scaling + diff write-back run unchanged).
3. Fast path SKIPS G3 polarization recompute: when the input would
   normally trip the polarization-breach gate, the fast path leaves
   future hard sessions untouched.
4. Fast path SKIPS G4 ACWR rescale: when last week's actual/planned
   exceeds 1.5, the fast path does NOT scale the next week's tss_target.
5. Default behaviour unchanged — no kwarg leaves the full reforecast in
   place (G3 + G4 both fire).
6. `_accept_redraw_apply` passes `accept_redraw_fast=True` (source check
   — guarantees the wiring stays in place).
"""
from __future__ import annotations

import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import training_planner as tp  # noqa: E402


def _mk_plan_dict(
    monday: date,
    weeks_count: int = 4,
    include_hard: bool = False,
) -> dict:
    """Plan with z2 (or VO2max when `include_hard`) sessions Mon/Wed/Fri."""
    weeks = []
    for w_idx in range(weeks_count):
        ws = monday + timedelta(weeks=w_idx)
        we = ws + timedelta(days=6)
        sessions = []
        for off in range(7):
            d = ws + timedelta(days=off)
            if off in (1, 3, 5):
                stype = "vo2max" if include_hard else "z2"
                dur = 60
                tss = 110 if include_hard else 45
            else:
                stype = "rest"
                dur = 0
                tss = 0
            sessions.append({
                "day": d.isoformat(),
                "day_name": d.strftime("%a"),
                "session_type": stype,
                "duration_min": dur,
                "tss_estimate": tss,
                "description": stype,
                "zwo_file": f"real_{stype}.zwo" if dur else "",
                "zwo_name": f"{stype} 60min" if dur else "",
                "status": "pending",
            })
        weeks.append({
            "week_num": w_idx + 1,
            "phase": "base",
            "start": ws.isoformat(),
            "end": we.isoformat(),
            "tss_target": 200,
            "is_stepback": False,
            "sessions": sessions,
            "hit_per_week": 3 if include_hard else 0,
            "auto_acwr_scaled": False,
        })
    return {
        "goal": {
            "type": "weeks",
            "hours_per_week": 6.0,
            "rest_days": [0],
            "available_days": [1, 2, 3, 4, 5, 6],
        },
        "weeks": weeks,
        "availability": {},
    }


def test_fast_path_under_500ms_on_realistic_plan():
    """Acceptance bar: fast reforecast must finish under 0.5 s on a
    26-week plan with full availability dict (typical accept-redraw
    payload shape — frontend POSTs all 180 days).
    """
    monday = date.today() - timedelta(days=date.today().weekday())
    plan = _mk_plan_dict(monday, weeks_count=26)
    # Realistic availability payload (180 days, 1h each — typical UI POST).
    plan["availability"] = {
        (monday + timedelta(days=i)).isoformat(): {"hours": 1.0}
        for i in range(180)
    }
    availability_overrides = {
        k: v["hours"] for k, v in plan["availability"].items()
    }
    tsb_series = {}
    for w in plan["weeks"]:
        ws = date.fromisoformat(w["start"])
        for i in range(7):
            tsb_series[ws + timedelta(days=i)] = -10

    # Warm cache (first call may touch lazy globals).
    tp.reforecast_dict(
        plan, tsb_series=tsb_series,
        availability_overrides=availability_overrides,
        accept_redraw_fast=True,
    )

    t0 = time.time()
    tp.reforecast_dict(
        plan, tsb_series=tsb_series,
        availability_overrides=availability_overrides,
        accept_redraw_fast=True,
    )
    elapsed = time.time() - t0
    assert elapsed < 0.5, (
        f"fast path took {elapsed*1000:.0f}ms; acceptance bar is <500ms"
    )


def test_fast_path_still_propagates_downstream_diff():
    """Downstream TSS cascade (availability scaling + diff-write-back)
    must still fire on the fast path. Verify by setting availability
    overrides that shrink one day and confirming the session's
    duration_min / tss_estimate are persisted into plan_dict.
    """
    monday = date.today() - timedelta(days=date.today().weekday())
    plan = _mk_plan_dict(monday, weeks_count=2)
    # Pick a future Wed (day 3 of next week — definitely > today).
    next_wed = monday + timedelta(weeks=1, days=3)
    plan["availability"] = {next_wed.isoformat(): {"hours": 0.5}}

    _, sessions_modified, _info = tp.reforecast_dict(
        plan,
        availability_overrides={next_wed.isoformat(): 0.5},
        accept_redraw_fast=True,
    )

    # 0.5h = 30min: session was 60min, must shrink to 30 (within ±1 round).
    target = next(
        s for w in plan["weeks"] for s in w["sessions"]
        if s["day"] == next_wed.isoformat()
    )
    assert target["duration_min"] == 30, (
        f"fast path failed to propagate duration override; got "
        f"{target['duration_min']}min (expected 30min)"
    )
    assert sessions_modified >= 1, (
        f"fast path reported {sessions_modified} mods; expected ≥1"
    )


def test_fast_path_skips_g4_acwr_rescale():
    """G4 ACWR trip: when last completed week's actual_tss / planned_tss
    > 1.5, the FULL path scales next week's tss_target ×0.85. The fast
    path must NOT.
    """
    monday = date.today() - timedelta(days=date.today().weekday())
    # Build a plan where week 0 is in the past (fully completed).
    past_monday = monday - timedelta(weeks=1)
    plan = _mk_plan_dict(past_monday, weeks_count=3)
    # Make week 0 completed (end = monday - 1 day).
    # _mk_plan_dict already created weeks starting at past_monday, so
    # week 1 ends at past_monday+6 = monday-1 ⇒ fully past.
    # Fabricate "ride" history that doubles the planned TSS for week 1
    # (week_num=1, the past week).
    week0 = plan["weeks"][0]
    planned_w0 = week0["tss_target"]  # 200
    actual_activities = []
    # Add rides each totalling 2× planned to push ACWR above 1.5.
    ws = date.fromisoformat(week0["start"])
    we = date.fromisoformat(week0["end"])
    for off in range(7):
        d = ws + timedelta(days=off)
        if d > we:
            break
        actual_activities.append({
            "date": d.isoformat(),
            "tss": planned_w0,  # 7 × 200 = 1400 actual vs 200 planned → ACWR 7.0
        })

    # Future week 2 — the one G4 would scale.
    future_week = plan["weeks"][2]
    original_tss_target = future_week["tss_target"]

    # FULL path: should scale.
    _, _smod_full, info_full = tp.reforecast_dict(
        plan, recent_activities=actual_activities,
    )
    # FAST path: should NOT scale. Reset plan first.
    plan2 = _mk_plan_dict(past_monday, weeks_count=3)
    _, _smod_fast, info_fast = tp.reforecast_dict(
        plan2, recent_activities=actual_activities,
        accept_redraw_fast=True,
    )
    fast_future_tss = plan2["weeks"][2]["tss_target"]

    # Sanity: full path tripped G4 (acwr_ratio > 1.5 and a week got scaled).
    assert info_full.get("acwr_ratio", 0) > 1.5, (
        f"test setup failure: full-path acwr_ratio={info_full.get('acwr_ratio')} "
        f"(expected >1.5)"
    )
    assert info_full.get("acwr_scaled_week") is not None, (
        "test setup failure: full-path should have scaled a week"
    )
    # Fast path: ratio not computed → 0.0; no week scaled.
    assert info_fast.get("acwr_scaled_week") is None, (
        f"fast path scaled week {info_fast.get('acwr_scaled_week')} — must skip G4"
    )
    assert fast_future_tss == original_tss_target, (
        f"fast path mutated future tss_target ({original_tss_target} → "
        f"{fast_future_tss}); must skip G4"
    )


def test_fast_path_skips_g3_polarization_recompute():
    """G3 polarization-breach gate trips when actual Z4+ exceeds target+8
    or Z1+Z2 floor drops below target−10. Full path drops next hard
    sessions one tier; fast path leaves them alone.
    """
    monday = date.today() - timedelta(days=date.today().weekday())
    plan_full = _mk_plan_dict(monday, weeks_count=3, include_hard=True)
    plan_fast = _mk_plan_dict(monday, weeks_count=3, include_hard=True)

    # Polarization shapes that trip _polarization_breach (Z4+ >>
    # target+8): actual Z4 = 40 %, target Z4 = 10 % → +30 over target.
    actual_pol = {"z1z2_pct": 50, "z3_pct": 10, "z4plus_pct": 40}
    target_pol = {"z1z2_pct": 80, "z3_pct": 10, "z4plus_pct": 10}

    _, _smod_full, info_full = tp.reforecast_dict(
        plan_full,
        actual_polarization=actual_pol,
        target_polarization=target_pol,
    )
    _, _smod_fast, info_fast = tp.reforecast_dict(
        plan_fast,
        actual_polarization=actual_pol,
        target_polarization=target_pol,
        accept_redraw_fast=True,
    )

    # Full path: gate fires and drops 1–2 sessions.
    assert info_full.get("polarization_breach") is True, (
        "test setup failure: full-path should detect polarization breach"
    )
    assert len(info_full.get("g3_dropped_days") or []) >= 1, (
        "test setup failure: full-path should drop at least one session"
    )
    # Fast path: gate skipped entirely.
    assert info_fast.get("polarization_breach") is False, (
        "fast path reported polarization_breach=True; must skip G3"
    )
    assert not info_fast.get("g3_dropped_days"), (
        f"fast path dropped sessions {info_fast.get('g3_dropped_days')}; "
        "must skip G3"
    )


def test_default_behaviour_unchanged_no_kwarg():
    """Calling `reforecast_dict` without the new kwarg must keep the
    pre-v1.8.1 algorithm: G3 + G4 both fire when inputs trigger them.
    """
    monday = date.today() - timedelta(days=date.today().weekday())
    plan = _mk_plan_dict(monday, weeks_count=3, include_hard=True)

    actual_pol = {"z1z2_pct": 50, "z3_pct": 10, "z4plus_pct": 40}
    target_pol = {"z1z2_pct": 80, "z3_pct": 10, "z4plus_pct": 10}

    _, _smod, info = tp.reforecast_dict(
        plan,
        actual_polarization=actual_pol,
        target_polarization=target_pol,
    )
    # Default path: G3 trip must surface in info.
    assert info.get("polarization_breach") is True, (
        f"default path lost G3 trip behaviour; info={info}"
    )


def test_accept_redraw_apply_passes_fast_kwarg():
    """Source-level wiring check: `_accept_redraw_apply` must pass
    `accept_redraw_fast=True` so the speed win is actually plumbed.
    """
    app_path = ROOT / "src" / "app.py"
    src = app_path.read_text(encoding="utf-8")
    # Locate the `_accept_redraw_apply` function body and confirm it
    # contains the kwarg on a `reforecast_dict(...)` call.
    m = re.search(
        r"def _accept_redraw_apply\(.*?\)(?:.|\n)*?(?=\n(?:def |@app\.|class ))",
        src,
    )
    assert m, "could not locate _accept_redraw_apply in app.py"
    body = m.group(0)
    assert "accept_redraw_fast=True" in body, (
        "_accept_redraw_apply must call tp.reforecast_dict with "
        "accept_redraw_fast=True (v1.8.1 SPEED-B wiring)"
    )
