"""Do the five planner entry points obey the same rules?

Each of generate / regenerate / recalculate / extend / refit assembles its own
sequence of enforcement passes -- measured, 12, 5, 6, 6 and 3 of them. This
probe asks whether that difference is visible in the output: it builds many
riders, runs each entry point, and audits every resulting plan against
plan_invariants.

Not a unittest: it is a measurement, and its job is to say how big the problem
is before the architecture changes, and that it stayed fixed after.

Run:  .venv/bin/python tests/probe_entry_point_parity.py [n_riders]
"""
import copy
import datetime as dt
import itertools
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import training_planner as tp          # noqa: E402
import plan_invariants as pi           # noqa: E402

TODAY = dt.date(2026, 9, 10)


def riders(n, rng):
    """Plausible athletes, not adversarial ones -- the point is whether the
    entry points disagree for ordinary riders, not whether they survive junk."""
    for _ in range(n):
        n_avail = rng.randint(3, 7)
        avail = sorted(rng.sample(range(7), n_avail))
        rest = [d for d in range(7) if d not in avail]
        cap = {d: rng.choice([1.0, 1.5, 2.0, 3.0, 4.0]) for d in avail}
        yield tp.Goal(
            goal_type="continuous", rest_days=rest, available_days=avail,
            daily_max_hours=cap,
            hours_per_week=rng.choice([6, 8, 10, 12, 15, 18]),
            max_weekday_hours=max(cap.values()),
            max_weekend_hours=max(cap.values()),
            plan_weeks=12,
        ), rng.randint(0, 9999), rng.choice([25.0, 37.1, 55.0, 75.0])


def run_all(goal, salt, ctl):
    """Every entry point, from the same base plan. Returns name -> weeks|Exception."""
    out = {}
    try:
        _ph, base = tp.generate_plan(goal, seed_salt=salt, current_ctl=ctl)
        out["generate"] = base
    except Exception as e:                                    # noqa: BLE001
        return {"generate": e}
    cp = lambda: [copy.deepcopy(w) for w in base]             # noqa: E731
    for name, fn in (
        ("regenerate", lambda: tp.regenerate_from_today(goal, cp(), ctl, seed_salt=salt)[1]),
        ("recalculate", lambda: tp.recalculate_plan(goal, cp(), ctl)[1]),
        ("extend", lambda: tp.extend_continuous_plan(goal, cp(), ctl, seed_salt=salt)[1]),
        ("refit", lambda: tp.refit_remaining_week(goal, cp(), TODAY, seed_salt=salt)[0]),
    ):
        try:
            out[name] = fn()
        except Exception as e:                                # noqa: BLE001
            out[name] = e
    return out


def main(n=40):
    rng = random.Random(20260910)
    tally = {}          # entry -> rule -> riders affected
    errors = {}
    for goal, salt, ctl in riders(n, rng):
        for name, weeks in run_all(goal, salt, ctl).items():
            slot = tally.setdefault(name, {})
            if isinstance(weeks, Exception):
                errors.setdefault(name, []).append(f"{type(weeks).__name__}: {weeks}")
                slot["RAISED"] = slot.get("RAISED", 0) + 1
                continue
            for rule in {v.rule for v in pi.audit(weeks, goal)}:
                slot[rule] = slot.get(rule, 0) + 1

    rules = sorted({r for d in tally.values() for r in d})
    order = ["generate", "regenerate", "recalculate", "extend", "refit"]
    w = max((len(r) for r in rules), default=10) + 2
    print(f"\n{n} riders x 5 entry points -- riders with at least one violation\n")
    print(f"{'rule':<{w}}" + "".join(f"{o:>13}" for o in order))
    print("-" * (w + 13 * len(order)))
    for r in rules:
        print(f"{r:<{w}}" + "".join(f"{tally.get(o, {}).get(r, 0):>13}" for o in order))
    if not rules:
        print("(no violations anywhere)")
    for name, errs in errors.items():
        u = sorted(set(errs))
        print(f"\n{name} raised on {len(errs)} riders: {u[:3]}")
    return tally


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
