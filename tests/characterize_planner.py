#!/usr/bin/env python3
"""Pin the planner's current output so a refactor can be shown not to change it.

WHY. src/app.py is 22k lines and src/training_planner.py 14k, and the session
sizing invariant is currently re-derived in four separate places. Any
decomposition of those files has to answer one question on every commit: did
the plan a rider receives change? Unit tests answer "is this function right";
this answers "did anything move", which is the question a refactor is judged on.

HOW. A fixed matrix of goals, availabilities, targets and completed load is run
through the real planner with pinned dates and a pinned seed, and each week is
reduced to a fingerprint: the ordered (type, minutes, TSS) of its sessions plus
the week's target. Those fingerprints are stored in characterization.json.

    tests/characterize_planner.py            compare against the golden file
    tests/characterize_planner.py --bless    record current output as golden

--bless is for INTENTIONAL behaviour changes only, and the diff it replaces
should be read first: every blessed line is a change to what somebody is told
to ride. A refactor that needs --bless is not a refactor.

The planner is deterministic for a fixed seed_salt (verified), and every date
here is absolute rather than relative to today, so a run in six months compares
equal to a run now.
"""
import json
import os
import pathlib
import sys
from datetime import date, timedelta

# Re-exec with hash randomisation off before importing the planner.
#
# The HIT-variant shuffle IS seeded (training_planner.py:3496 builds a
# random.Random from an explicit seed), but the candidate list it shuffles
# arrives in set-iteration order, which changes per process. So the seeding is
# defeated by unordered input and two identical regenerations produce different
# plans -- 43 of 57 cases here differed run to run until this was pinned.
#
# Pinning makes characterization possible. It does NOT fix the app: /api/plan/
# regenerate is still irreproducible in production, which is worth its own fix
# (sort the candidates before seeding, rather than relying on set order).
# PYTHONDONTWRITEBYTECODE goes with it: a stale .pyc silently served a MUTATED
# planner while the source on disk read correct, and the harness dutifully
# reported 39 changed cases against a file that had already been restored. That
# will happen constantly if agents are editing rapidly, so never leave one.
# Only when RUN, never on import: `python -c "import characterize_planner"`
# has sys.argv == ["-c"], so re-execing at import time relaunches the
# interpreter with no code to run. tests/probe_plan_reproducibility.py imports
# this module precisely to run it WITHOUT the pin, and would have been
# silenced by it.
if __name__ == "__main__" and os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
import training_planner as tp  # noqa: E402

GOLDEN = HERE / "characterization.json"

# A Monday, fixed forever so the fingerprints do not drift with the calendar.
ANCHOR = date(2026, 9, 14)
SEED = 20260914

SESSION_TYPES = ["z2", "threshold", "vo2max", "sweetspot", "overunder", "tempo"]


def _goal(rest_days, hours, goal_type="continuous"):
    avail = [d for d in range(7) if d not in rest_days]
    return tp.Goal(
        goal_type=goal_type, rest_days=list(rest_days), available_days=avail,
        daily_max_hours={d: hours for d in avail},
    )


def _phase(target, name="continuous"):
    return tp.Phase(
        name=name, start=ANCHOR, end=ANCHOR + timedelta(weeks=6), weeks=6,
        focus="", weekly_tss_target=target, z2_pct=78, hit_per_week=2,
        session_types=list(SESSION_TYPES),
    )


def _fingerprint(week):
    return {
        "target": round(float(week.tss_target or 0), 1),
        "sessions": [
            [s.session_type, int(s.duration_min or 0), round(float(s.tss_estimate or 0), 1)]
            for s in week.sessions
        ],
    }


def cases():
    """The matrix. Each entry is (name, callable -> PlannedWeek)."""
    out = []

    # Availability shapes. The Mon-Fri rider is the reported configuration; the
    # weekend rider exercises the long-ride branch, which is the only one that
    # was ever budget-aware.
    shapes = [
        ("mon-fri", (5, 6)),
        ("weekend-rider", (0,)),
        ("every-day", ()),
        ("three-day", (2, 4, 5, 6)),
    ]
    for hours in (1.0, 2.0, 3.0):
        for sname, rest in shapes:
            for target in (150, 272, 450):
                key = f"week/{sname}/{hours:g}h/target{target}"
                out.append((key, lambda r=rest, h=hours, t=target: tp.plan_week(
                    1, ANCHOR, _phase(t), _goal(r, h), False, seed_salt=SEED)))

    # Stepback weeks: the unload logic, which was inverted before the fix.
    for sname, rest in shapes:
        out.append((f"stepback/{sname}", lambda r=rest: tp.plan_week(
            4, ANCHOR, _phase(272), _goal(r, 3.0), True, seed_salt=SEED)))

    # Load already ridden: nothing, part, exactly on target, well over.
    for done in (0, 80, 272, 400):
        out.append((f"completed/{done}", lambda c=done: tp.plan_week(
            1, ANCHOR, _phase(272), _goal((5, 6), 3.0), False,
            seed_salt=SEED, completed_tss=c)))

    # Week position matters: HIT rotation and stepback flavour key off week_num.
    for wk in (1, 2, 3, 5, 8):
        out.append((f"weeknum/{wk}", lambda w=wk: tp.plan_week(
            w, ANCHOR, _phase(272), _goal((5, 6), 3.0), False, seed_salt=SEED)))

    # The second builder. Same domain question, different implementation, so it
    # needs its own fingerprints or a refactor could quietly change one only.
    for sname, rest in shapes:
        for target in (200, 350):
            key = f"weekly/{sname}/target{target}"
            out.append((key, lambda r=rest, t=target: tp.generate_weekly_plan(
                goal=_goal(r, 3.0), current_phase=_phase(t), current_ctl=40.0)))

    return out


def collect():
    got = {}
    for name, fn in cases():
        try:
            got[name] = _fingerprint(fn())
        except Exception as e:  # a case that raises IS characterization
            got[name] = {"error": f"{type(e).__name__}: {e}"}
    return got


def main():
    bless = "--bless" in sys.argv
    got = collect()

    if bless or not GOLDEN.exists():
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        verb = "blessed" if GOLDEN.exists() and bless else "created"
        print(f"  {verb} {GOLDEN.name}: {len(got)} cases")
        return 0

    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    added = sorted(set(got) - set(want))
    removed = sorted(set(want) - set(got))
    changed = sorted(k for k in set(got) & set(want) if got[k] != want[k])

    if not (added or removed or changed):
        print(f"  {len(got)} cases, all unchanged")
        return 0

    print(f"  {len(changed)} changed, {len(added)} added, {len(removed)} removed "
          f"(of {len(want)} golden cases)\n")
    for k in changed[:12]:
        print(f"  ── {k}")
        w, g = want[k], got[k]
        if w.get("target") != g.get("target"):
            print(f"       target  {w.get('target')} -> {g.get('target')}")
        ws, gs = w.get("sessions", []), g.get("sessions", [])
        for i in range(max(len(ws), len(gs))):
            a = ws[i] if i < len(ws) else None
            b = gs[i] if i < len(gs) else None
            if a != b:
                print(f"       day {i}   {a} -> {b}")
    if len(changed) > 12:
        print(f"  ... and {len(changed) - 12} more")
    for k in added:
        print(f"  ++ {k}")
    for k in removed:
        print(f"  -- {k}")
    print("\n  If every change above is intended, re-run with --bless.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
