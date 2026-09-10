#!/usr/bin/env python3
"""What weekday does each planned week actually start on?

Every rollup in the app aggregates Monday-Sunday: the week tile, the adherence
counter, the TSS budget, the ramp-rate check. The planner does not. Phases are
allocated backwards from the event date, and each week cursor then walks in
7-day steps from `phase.start`, so a plan built for a Saturday event has
Saturday-Friday weeks, and a regenerate run on a Wednesday has Wednesday-Tuesday
weeks -- both silently misaligned with everything that reads them.

This prints the truth rather than arguing about it. Run it before and after any
anchoring change:

    tests/probe_week_anchors.py

Each row is one plan: the builder, the seed date, and the weekday of every
week start it emitted. A Monday-anchored planner prints Mon for all of them
except the first, which is short by construction when the plan is generated
mid-week -- the count column ignores that one and flags every later week.
"""
import os
import pathlib
import sys
from datetime import date, timedelta

if os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
import training_planner as tp  # noqa: E402

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _goal(target_date, goal_type="event", rest=(5, 6), hours=2.0):
    avail = [d for d in range(7) if d not in rest]
    return tp.Goal(
        goal_type=goal_type, rest_days=list(rest), available_days=avail,
        daily_max_hours={d: hours for d in avail},
        target_date=target_date, event_name="probe",
    )


def _starts(weeks):
    return [w.start for w in weeks]


def _row(label, starts):
    """Counts non-Monday starts AFTER the first week.

    The opening week is short by design: a plan generated on a Thursday runs
    Thu-Sun and then goes Monday-Sunday forever after. Leaving Thu-Sun blank so
    the plan could open on a Monday would be a different, worse change -- the
    rider asked for a plan today.
    """
    wd = [DAYS[d.weekday()] for d in starts]
    off = sum(1 for d in starts[1:] if d.weekday() != 0)
    tail = " ".join(wd[:12]) + (" …" if len(wd) > 12 else "")
    print(f"  {label:<44} {len(starts):>3}w  {off:>3} late non-Mon   {tail}")
    return off


def main():
    today = date.today()
    total_off = 0
    total_weeks = 0

    print("\ngenerate_plan — event on each weekday, 10 weeks out")
    for i in range(7):
        # A target date landing on each weekday in turn.
        tgt = today + timedelta(weeks=10)
        tgt = tgt + timedelta(days=(i - tgt.weekday()) % 7)
        try:
            _, weeks = tp.generate_plan(_goal(tgt), seed_salt=1)
        except Exception as e:
            print(f"  event {DAYS[i]:<38} ERROR {type(e).__name__}: {e}")
            continue
        s = _starts(weeks)
        total_off += _row(f"event on {DAYS[i]} ({tgt})", s)
        total_weeks += len(s)

    print("\nregenerate_from_today — rebuild an existing plan")
    tgt = today + timedelta(weeks=12)
    tgt = tgt + timedelta(days=(5 - tgt.weekday()) % 7)   # a Saturday event
    goal = _goal(tgt)
    try:
        _, base = tp.generate_plan(goal, seed_salt=1)
        _, weeks, _ = tp.regenerate_from_today(goal, base, current_ctl=45.0, seed_salt=1)
        s = _starts(weeks)
        total_off += _row(f"regen today={DAYS[today.weekday()]} ({today})", s)
        total_weeks += len(s)
    except Exception as e:
        print(f"  regen                                       ERROR {type(e).__name__}: {e}")

    # plan_week is a primitive: it builds cursor..cursor+6 from whatever it is
    # handed, and that is right -- the anchoring belongs to the callers, which
    # now always hand it a Monday. Printed so the split of responsibility is
    # visible rather than assumed.
    print("\nplan_week — a primitive: honours its anchor, does not impose one")
    for i in range(7):
        anchor = today + timedelta(days=(i - today.weekday()) % 7)
        phase = tp.Phase(name="continuous", start=anchor, end=anchor + timedelta(weeks=4),
                         weeks=4, focus="", weekly_tss_target=300, z2_pct=78,
                         hit_per_week=2, session_types=["z2", "threshold", "vo2max"])
        pw = tp.plan_week(1, anchor, phase, _goal(None, "continuous"), False, seed_salt=1)
        got = pw.start.weekday()
        flag = "" if got == 0 else "  (caller's anchor, by design)"
        print(f"  plan_week(anchor={DAYS[i]})              -> starts {DAYS[got]}{flag}")

    print(f"\n  TOTAL: {total_off} of {total_weeks} week starts after the opening "
          f"week are not a Monday\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
